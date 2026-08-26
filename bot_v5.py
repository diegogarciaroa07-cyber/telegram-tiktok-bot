import bot_v2
import bot_v3
import bot_v4


# =========================================================
# MODO SEGURO TEMPORAL PARA INSTAGRAM
# - Reels y publicaciones: activos
# - Stories normales: desactivadas
# - Historias destacadas: desactivadas
# =========================================================


def _es_historia_instagram(url):
    url = str(url or "").strip().lower()
    return (
        "instagram.com/stories/" in url
        or "instagr.am/stories/" in url
        or "instagram.com/s/" in url
        or "instagr.am/s/" in url
    )


def es_instagram(url):
    """Acepta Instagram excepto Stories y Highlights mientras estén pausadas."""
    url = str(url or "").strip().lower()
    if not ("instagram.com/" in url or "instagr.am/" in url):
        return False
    if _es_historia_instagram(url):
        return False
    return True


def descargar_instagram(url, directorio):
    # Bloqueo inmediato: no hacemos ninguna petición a Instagram para links
    # que ya sabemos que son Stories o Highlights.
    if _es_historia_instagram(url):
        raise RuntimeError("Historias de Instagram desactivadas temporalmente")

    # Conservamos compatibilidad con links compartidos de Reels/publicaciones.
    url_resuelta = bot_v3.resolver_url_instagram(url)
    print(f"Instagram: URL normalizada = {url_resuelta}", flush=True)

    # Si un link compartido genérico termina resolviendo a una Story/Highlight,
    # lo detenemos antes de iniciar cualquier descarga de historia.
    if _es_historia_instagram(url_resuelta) or bot_v3._es_highlight(url_resuelta):
        raise RuntimeError("Historias de Instagram desactivadas temporalmente")

    # Reels, videos, fotos, publicaciones y carruseles siguen con la lógica
    # existente; no usamos el flujo de Stories de bot_v4.
    return bot_v3._original_descargar_instagram(url_resuelta, directorio)


# Aplicamos el modo temporal al bot completo (Shortcut + Telegram).
bot_v2.es_instagram = es_instagram
bot_v2.descargar_instagram = descargar_instagram

main = bot_v2.main
iniciar_web = bot_v2.iniciar_web
