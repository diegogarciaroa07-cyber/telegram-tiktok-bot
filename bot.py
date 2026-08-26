# Punto de entrada estable.
# La versión anterior queda guardada en el historial de Git.
from bot_v2 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
