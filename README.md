# GNLT EV Charger for Home Assistant

**English** · [Polski](README.pl.md) · [Русский](README.ru.md)

Integration of GNLT EV chargers (portable and wall-mounted) with Home Assistant. The charger works with Home Assistant
directly in your home network, without a cloud.

> **Important.** The charger works either with the GNLT app or with Home Assistant, not with both at the same time.
> After you connect it to Home Assistant, the charger disappears from the GNLT app. How to bring it back — see
> [Back to the GNLT app](#back-to-the-gnlt-app).

## Features

- Start and stop charging, current limit (can be changed during charging)
- Power, current, voltage, energy of the charge and total energy — for the Home Assistant Energy dashboard
- Charging schedule, electricity price (single or two-rate tariff), cost of charging, totals for the day and the month
- Warnings: "The car did not start charging", "Charging started by the charger itself", "Connector out of service"
- Connecting the charger over Bluetooth or manually
- Languages: English, Polish, Russian

## What you need

- Home Assistant 2025.3 or newer.
- A fixed IP address of the computer or server running Home Assistant in your home network. Reserve it in your
  router settings.
- Free port 9000 on the computer or server running Home Assistant. If Home Assistant runs in Docker, start the
  container with `network_mode: host` or with `-p 9000:9000`. Do not open this port to the internet.
- A 2.4 GHz Wi-Fi network with WPA2 security. 5 GHz networks, WPA3 and mixed modes (WPA/WPA2, WPA2/WPA3) will not
  work. The network name and password may contain only Latin letters without accents, digits and symbols.
- To connect over Bluetooth: Bluetooth in Home Assistant near the charger — built-in, a USB adapter or an ESPHome
  Bluetooth Proxy.
- To connect manually: the GNLT app on your phone.

## Installation

1. Open **HACS**.
2. In the **⋮** menu choose **Custom repositories**.
3. Paste `https://github.com/GNLT-EV/gnlt-home-assistant`, choose **Integration** in the **Type** field and click
   **Add**. Close the window.
4. Type `GNLT EV Charger` in the HACS search and open the integration.
5. Click **Download**, then **Download** again in the window that opens.
6. Restart Home Assistant: **Settings** → **System** → **⏻** → **Restart Home Assistant** → **Restart**.

New versions will appear in **Settings** → **Updates**, like other Home Assistant updates.

## Connecting the charger over Bluetooth

1. On the charger press the **OK** button, choose **Wi-Fi**, switch **OCPP** off and leave the menu.
2. In Home Assistant open **Settings** → **Devices & services** → **Add integration**.
3. Type `GNLT` in the search and choose **GNLT EV Charger**.
4. Choose **Set up a charger over Bluetooth (recommended)**.
5. Select your charger — its serial number is on the housing — and click **Submit**.
6. Fill in the fields and click **Submit**:
   - **Network name** and **Network password** — your Wi-Fi network;
   - **Home Assistant address in your network** — the IP address of the computer or server running Home Assistant,
     for example `192.168.1.10`. If the field shows a different address, delete it and enter yours. If you open
     Home Assistant in the browser at an address like `http://192.168.1.10:8123`, your address is `192.168.1.10`.
     Otherwise find it in your router's list of devices;
   - **Port for chargers**, if this field is shown — `9000`;
   - **Start charging by command, not by plugging in** — leave it switched on.
7. Wait about half a minute while the charger saves the settings. Stay near the charger.
8. Check that your charger version is selected — the number of phases and the power, as on the charger's rating
   plate. If a different one is selected, select yours. Click **Submit**.
9. On the charger wait for the Wi-Fi icon on the screen. Press **OK**, choose **Wi-Fi**, switch **OCPP** on and leave
   the menu.
10. In Home Assistant click **Submit** and wait until the charger connects.
11. In the last window click **Skip and finish**.

The charger appears in **Settings** → **Devices & services** → **GNLT EV Charger**.

If the charger is not in the list at step 5: check that OCPP is off on the charger, close the GNLT app on your phone
and click **Submit** to search again.

## Connecting the charger manually

Use this way if Home Assistant has no Bluetooth.

1. Connect the charger to your Wi-Fi in the GNLT app: tap the **QR code** icon or **+**, scan the charger's QR code
   (in the charger manual and under the charger cover), enter the 6-digit PUK code (on the back of the manual) and
   confirm adding. Then tap the **Wi-Fi** icon and enter the network name and password.
2. In Home Assistant open **Settings** → **Devices & services** → **Add integration**.
3. Type `GNLT` in the search and choose **GNLT EV Charger**.
4. Choose **The charger is already on the network — enter its number**.
5. Enter the **Serial number** (12 digits from the charger housing), choose the **Charger version** — the number of
   phases and the power, as on the charger's rating plate — and click **Submit**.
6. Home Assistant shows the server address and the ChargeID. Keep this window open. If the address starts with
   `ws://172.`, close this window and open **Settings** → **System** → **Network**. In **Local network** switch
   **Automatic** off, enter `http://computer-IP-address:8123`, for example `http://192.168.1.10:8123`, click **Save**
   and start again from step 2.
7. On the charger press **OK** and choose **Wi-Fi**. Switch **OCPP** off, enter the server address and the ChargeID
   from the Home Assistant window, switch **OCPP** on and leave the menu.
8. In Home Assistant click **Submit**, then in the last window **Skip and finish**.

## Back to the GNLT app

1. In Home Assistant open **Settings** → **Devices & services** → **GNLT EV Charger**.
2. Next to the charger click **⋮** → **Delete** and confirm.
3. On the charger press **OK**, choose **Wi-Fi**, switch **OCPP** off and leave the menu.
4. In the GNLT app on your phone add the charger again: tap the **QR code** icon or **+**, scan the charger's QR code,
   enter the PUK code and confirm adding.
5. On the charger press **OK**, choose **Wi-Fi**, switch **OCPP** on and leave the menu. The charger connects to the
   GNLT app.

## Good to know

- The names of the charger's sensors and switches are shown in the Home Assistant language set in the system settings.
- After a connection loss the charger is shown as connected for about two more minutes.
- If the car did not start charging, charging may start by itself when the car wakes up. If you do not want that,
  unplug the cable.
- Some switches may be grey: these features depend on the charger's firmware version.

## If something goes wrong

- **No Wi-Fi icon on the charger.** Check the network name, the password and the router settings: 2.4 GHz network,
  WPA2 security without a mixed mode. Then connect the charger again.
- **The Wi-Fi icon is there, but the charger does not connect to Home Assistant.** Check that OCPP is on on the
  charger, the Home Assistant address is correct and port 9000 is free.
- **The charger is not visible over Bluetooth.** Switch OCPP off on the charger, close the GNLT app on your phone and
  move the Bluetooth adapter closer to the charger.

## Support

GNLT · info@gnlt.pl · +48 505 699 273 · https://gnlt.pl

Issues: https://github.com/GNLT-EV/gnlt-home-assistant/issues
