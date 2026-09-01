# Punto de entrada estable.
# bot_v6 mantiene Instagram en modo seguro y añade recuperación de TikTok.
from bot_v6 import main, iniciar_web
import threading

if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
