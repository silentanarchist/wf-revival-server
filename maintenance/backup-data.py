"""Nightly backup of the server's data (accounts, players, decks, bans, chat, custom cards).

Makes one dated zip of data/ (plus the live settings file) in the backup folder, checks that
every file in it reads back, and writes a line to backup-log.txt there. Old backups are kept;
nothing is ever deleted (a day's backup is about 50 KB).

    python maintenance/backup-data.py [backup folder]      (default C:\\WarpforgeRevival-backups)

Restore: stop the server, unzip a backup's "data" folder over the server's data folder (and
"revival-settings.json" into content\\files if wanted), start the server.
"""
import datetime
import json
import os
import sys
import time
import zipfile

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(SERVER, "data")
SETTINGS = os.path.join(SERVER, "content", "files", "revival-settings.json")
DEST = sys.argv[1] if len(sys.argv) > 1 else r"C:\WarpforgeRevival-backups"


def read(path):
    """A file's bytes; the server replaces files in one step, so a busy file is only retried."""
    for attempt in range(5):
        try:
            with open(path, "rb") as f:
                return f.read()
        except PermissionError:
            time.sleep(1)
    with open(path, "rb") as f:
        return f.read()


def main():
    os.makedirs(DEST, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(DEST, "warpforge-data-%s.zip" % stamp)
    n = 2
    while os.path.exists(target):                    # never overwrite an earlier backup
        target = os.path.join(DEST, "warpforge-data-%s-%d.zip" % (stamp, n))
        n += 1
    log = os.path.join(DEST, "backup-log.txt")
    files = 0
    tmp = target + ".tmp"
    try:
        if not os.path.isdir(DATA):
            raise RuntimeError("no data folder at " + DATA)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, names in os.walk(DATA):
                dirs.sort()
                for name in sorted(names):
                    if name.endswith(".tmp"):
                        continue                    # a file the server is writing at this moment
                    full = os.path.join(root, name)
                    z.writestr(os.path.join("data", os.path.relpath(full, DATA)), read(full))
                    files += 1
            if os.path.isfile(SETTINGS):
                z.writestr("revival-settings.json", read(SETTINGS))
                files += 1
        # check: every file reads back, and every .json in it is valid JSON
        with zipfile.ZipFile(tmp) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError("damaged in the zip: " + bad)
            for name in z.namelist():
                if name.endswith(".json"):
                    try:
                        json.loads(z.read(name).decode("utf-8"))
                    except ValueError:
                        raise RuntimeError("not valid JSON: " + name)
        os.replace(tmp, target)
        line = "%s  OK     %d files, %.1f KB  %s" % (stamp, files, os.path.getsize(target) / 1024, os.path.basename(target))
        ok = True
    except Exception as e:
        line = "%s  FAILED %s" % (stamp, e)
        ok = False
        try:
            os.remove(tmp)                           # only this run's unfinished zip
        except OSError:
            pass
    with open(log, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
