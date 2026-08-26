# Punto de entrada estable.
# bot_v3 añade soporte para links compartidos de Instagram sin tocar la lógica estable anterior.
from bot_v3 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
