# Punto de entrada estable.
# bot_v8 intenta TikWM con impersonación de navegador y conserva Instagram seguro.
from bot_v8 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
