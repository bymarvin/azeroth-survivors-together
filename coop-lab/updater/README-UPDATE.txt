Azeroth Survivors Together - co-op update to 0.95.0 (co-op 0.0.11)

Updates an existing Azeroth Survivors Together 0.92.0, 0.93.1 or 0.95.0
co-op folder. Everyone in a party needs the same version.

How to update
1. Close the game and the co-op launcher.
2. Extract this zip into your game folder (the folder with Start-Coop.cmd).
3. Double-click Update-Coop.cmd and wait for "Installed co-op version: 0.0.11".

What it does
- Recognises your version from package-manifest.json and the game exe.
- Refuses (and changes nothing) if the game is still running, a file is
  locked, the disk is full or the folder is unknown or modified.
- Backs up every file it replaces into coop-update-backup-<date> and
  restores everything automatically if anything goes wrong.
- Only touches files of the release itself. Your saves, profiles, addons,
  WTF settings and logs are never changed.

After a successful update you can delete coop-update-backup-<date>,
the coop-update folder, Update-Coop.cmd and this file.

Too old? Co-op 0.0.7 to 0.0.10 for game 0.84 cannot be updated this way;
download the full Azeroth Survivors Together 0.95.0 package instead.
