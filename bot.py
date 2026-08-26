# Punto de entrada estable.
# bot_v4 corrige Stories normales usando el media ID directo.
from bot_v4 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
