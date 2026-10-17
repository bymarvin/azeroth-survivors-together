# Azeroth Survivors Together - co-op updates

This repository only hosts releases of the Azeroth Survivors Together co-op mod updater.

## Update your game folder
1. Download **Azeroth-Survivors-Together-Updater.exe** from the [latest release](../../releases/latest).
2. Put it into your Azeroth Survivors Together co-op folder (where `Start-Coop.cmd` is).
3. Close the game and the co-op launcher, then double-click the updater.

The updater checks a signed release description, downloads only the files that changed,
backs up everything it replaces and restores it automatically if anything fails. Your saves,
addons and WTF settings are never touched. Keep the updater in your game folder: it updates
itself, and the co-op launcher tells you when a new version is available.

Everyone in a party needs the same co-op version.

Windows may warn about an unknown publisher because the updater is not code-signed:
choose "More info" -> "Run anyway".

## Full package
Your game files are too old for an automatic update. Get the full package from the link in the Discord
release thread, unpack it and start Start-Coop.cmd.
