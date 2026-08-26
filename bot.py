# Punto de entrada estable.
# bot_v5 mantiene Reels/publicaciones y pausa Stories/Highlights temporalmente.
from bot_v5 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
