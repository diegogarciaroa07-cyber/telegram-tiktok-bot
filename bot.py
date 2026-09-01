# Punto de entrada estable.
# bot_v7 usa TikWM para TikTok y conserva Instagram en modo seguro.
from bot_v7 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
