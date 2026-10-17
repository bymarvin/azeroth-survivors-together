# Azeroth Survivors Together - co-op updates

This repository only hosts releases of the Azeroth Survivors Together co-op mod installer and updater.

## Install
1. Download **Azeroth-Survivors-Together-Installer.exe** from the [latest release](../../releases/latest)
   (about 0.4 MB) and double-click it. No admin rights are needed.
2. Choose the folder (default: `%LOCALAPPDATA%\Azeroth Survivors Together`). The installer
   checks the signed release, downloads everything (about 1.4 GB, interrupted downloads
   continue), checks every file and offers Start menu and desktop shortcuts.
3. Start the game with **Start-Coop.cmd** (or the shortcut). It keeps itself up to date.

Running the installer on a folder that already holds the game updates it instead of
reinstalling. You need about 3 GB of free space.

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

Windows SmartScreen may warn about an unknown publisher because the installer and the
updater are not code-signed: choose "More info" -> "Run anyway". Every download they make
is checked against a signed release description and SHA-256 hashes.

## Full package
Your game files are too old for an automatic update. Get the full package from the link in the Discord
release thread, unpack it and start Start-Coop.cmd.
