# Azeroth Survivors Together - co-op updates

This repository only hosts releases of the Azeroth Survivors Together co-op mod installer and updater.

## Install
1. Download **Azeroth-Survivors-Together-Installer.exe** from the [latest release](../../releases/latest)
   (about 0.4 MB), put it where the game should go (e.g. `D:\Games`) and double-click it.
   No questions, no admin rights.
2. It installs into the folder **Azeroth Survivors Together** next to itself: it checks the
   signed release, downloads everything (about 1.4 GB, interrupted downloads continue), checks
   every file, adds a desktop and Start menu shortcut and opens the folder.
3. Start the game with **Start-Coop.cmd** (or the shortcut). It keeps itself up to date.

Put the installer into a folder that already holds the game and it updates that folder instead.
You need about 3 GB of free space. Folders like Program Files need admin rights: the installer
then stops with a message, move it to a folder of your own.

## Update your game folder
1. Download **Azeroth-Survivors-Together-Updater.exe** from the [latest release](../../releases/latest).
2. Put it into your Azeroth Survivors Together co-op folder (where `Start-Coop.cmd` is).
3. Close the game and the co-op launcher, then double-click the updater.

The updater checks a signed release description, downloads only the files that changed,
backs up everything it replaces and restores it automatically if anything fails. Your saves,
addons and WTF settings are never touched. Keep the updater in your game folder: it updates
itself, and the co-op launcher tells you when a new version is available.

From co-op 0.0.13 on, `Start-Coop.cmd` installs new versions automatically before the
launcher starts (press Enter during the 5 second countdown to skip). It fetches the updater
by itself if it is missing. To switch this off, create the file `coop-update\no-auto-update`
in the game folder or set the environment variable `COOP_AUTO_UPDATE=0`.

Everyone in a party needs the same co-op version.

From co-op 0.0.20 on, `Start-Coop.cmd` also checks the files of an up-to-date folder against the
signed release. If one differs (for example another `AzerothSurvivors.exe` - the room then shows
"Game Unknown") or is missing, it is restored from the release with a backup and the console says
"Game files repaired". Saves, addons and WTF settings are never part of this. To run it by hand:
`Azeroth-Survivors-Together-Updater.exe --repair`.

Windows SmartScreen may warn about an unknown publisher because the installer and the
updater are not code-signed: choose "More info" -> "Run anyway". Every download they make
is checked against a signed release description and SHA-256 hashes.

## Play in your own network or over a VPN (co-op 0.0.20 and later)
Choose **LAN / VPN** when Start-Coop asks for the region (or start `Start-Coop.cmd lan`). No Internet
is needed and the game traffic stays in your network.
- **Host:** create a room as usual. The room browser shows this PC's address and a **LAN code**
  (for example `192.168.1.20 · K7QF-2M3D`).
- **Guests in the same network:** the host's room appears in the list by itself. Join it.
- **Guests over a VPN** (Tailscale, ZeroTier, Hamachi, ...): enter the host's VPN address with the LAN
  code in the LAN / VPN box, e.g. `100.101.1.2#K7QF-2M3D`, or start `Start-Coop.cmd lan 100.101.1.2#K7QF-2M3D`.
  The code proves you reached the right PC; the host's invite code already contains both.
- **Windows Firewall:** when Windows asks on the host PC, allow co-op (`.coop-runtime\python.exe`), also for
  "Public" networks if you use a VPN (VPN adapters are often classed as public). Ports: TCP 47770 (room list),
  TCP 47771 (game, see Connection settings) and UDP 47773 (finding hosts). Guest Wi-Fi networks often block
  devices from seeing each other; use the normal network or a VPN.
- Crash reports stay on your PC in LAN mode.

## Full package
The complete game folder of the newest release as one zip (about 1.4 GB), always at the same address:
**https://azsurvivors.duckdns.org/full/Azeroth-Survivors-Together.zip**
(SHA-256 next to it: `Azeroth-Survivors-Together.zip.sha256`). Unpack it into a folder of your own and start
Start-Coop.cmd. Use it when your game files are too old for an automatic update, or instead of the installer.
