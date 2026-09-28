"""
Lance bot_centrale.py et bot_sender.py en même temps, dans un seul terminal.

- Si un des deux plante, il est relancé automatiquement au bout de 5 secondes.
- Ctrl+C arrête proprement les deux.

Usage :  python run_all.py      (ou double-clic sur start.bat sous Windows)
"""
import os
import subprocess
import sys
import time

BOTS = ["bot_centrale.py", "bot_sender.py"]
RESTART_DELAY = 5


def lancer(script):
    return subprocess.Popen([sys.executable, script])


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    procs = {script: lancer(script) for script in BOTS}
    print(f"▶️  Lancé : {', '.join(BOTS)}  (Ctrl+C pour tout arrêter)")

    try:
        while True:
            time.sleep(2)
            for script, proc in list(procs.items()):
                if proc.poll() is not None:
                    print(f"[!] {script} s'est arrêté (code {proc.returncode}), "
                          f"redémarrage dans {RESTART_DELAY}s...")
                    time.sleep(RESTART_DELAY)
                    procs[script] = lancer(script)
    except KeyboardInterrupt:
        print("\n🛑 Arrêt des bots...")
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
