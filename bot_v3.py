import requests
import bot_v2


# =========================================================
# COMPATIBILIDAD CON LINKS COMPARTIDOS DE INSTAGRAM
# =========================================================

_original_descargar_instagram = bot_v2.descargar_instagram


def es_instagram(url):
    """Acepta posts, reels, stories y también links /share/ de Instagram."""
    url = str(url or "").strip().lower()
    return "instagram.com/" in url or "instagr.am/" in url


def resolver_url_instagram(url):
    """Convierte un link compartido de Instagram en su URL final /reel/, /p/ o /stories/."""
    url = str(url or "").strip()

    # Los links normales no necesitan resolución.
    if any(x in url.lower() for x in (
        "instagram.com/reel/",
        "instagram.com/reels/",
        "instagram.com/p/",
        "instagram.com/tv/",
        "instagram.com/stories/",
    )):
        return url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    }

    cookies = None
    try:
        cookies = bot_v2.leer_cookies_instagram()
    except Exception:
        pass

    try:
        r = requests.get(
            url,
            headers=headers,
            cookies=cookies,
            allow_redirects=True,
            timeout=20,
        )
        final_url = r.url or url
        print(f"Instagram: link resuelto a {final_url}")
        return final_url
    except Exception as e:
        print(f"Instagram: no se pudo resolver el link compartido: {e}")
        return url


def descargar_instagram(url, directorio):
    url_resuelta = resolver_url_instagram(url)
    return _original_descargar_instagram(url_resuelta, directorio)


# Monkey-patch controlado: mantenemos intacta toda la lógica estable de bot_v2.
bot_v2.es_instagram = es_instagram
bot_v2.descargar_instagram = descargar_instagram

# Reexportamos lo que usa bot.py
main = bot_v2.main
iniciar_web = bot_v2.iniciar_web
