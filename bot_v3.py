import base64
import re
from urllib.parse import parse_qs, urlparse, urlunparse

import requests
import bot_v2


# =========================================================
# COMPATIBILIDAD CON LINKS COMPARTIDOS / DESTACADAS DE IG
# =========================================================

_original_descargar_instagram = bot_v2.descargar_instagram


def es_instagram(url):
    """Acepta posts, reels, stories, highlights, /share/ y /s/ de Instagram."""
    url = str(url or "").strip().lower()
    return "instagram.com/" in url or "instagr.am/" in url


def _decodificar_link_s(url):
    """
    Instagram comparte algunas historias destacadas como /s/<base64>.
    El token suele decodificar a 'highlight:<id>'. Conservamos query params
    como story_media_id para poder descargar el elemento exacto.
    """
    try:
        parsed = urlparse(url)
        m = re.search(r"/s/([^/?#]+)", parsed.path, re.I)
        if not m:
            return None

        token = m.group(1)
        token += "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token.encode()).decode("utf-8", errors="ignore")

        m_highlight = re.search(r"highlight:(\d+)", decoded, re.I)
        if not m_highlight:
            return None

        highlight_id = m_highlight.group(1)
        return urlunparse((
            "https",
            "www.instagram.com",
            f"/stories/highlights/{highlight_id}/",
            "",
            parsed.query,
            "",
        ))
    except Exception as e:
        print(f"Instagram: no se pudo decodificar /s/: {e}")
        return None


def resolver_url_instagram(url):
    """Convierte enlaces compartidos de Instagram a una URL utilizable."""
    url = str(url or "").strip()
    lower = url.lower()

    # Link /s/ usado por la app para compartir highlights.
    if "instagram.com/s/" in lower:
        decodificada = _decodificar_link_s(url)
        if decodificada:
            print(f"Instagram: /s/ decodificado a {decodificada}")
            return decodificada

    # Links normales ya utilizables.
    if any(x in lower for x in (
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

        # Si el redirect termina en /s/, decodificamos también.
        if "instagram.com/s/" in final_url.lower():
            decodificada = _decodificar_link_s(final_url)
            if decodificada:
                final_url = decodificada

        print(f"Instagram: link resuelto a {final_url}")
        return final_url
    except Exception as e:
        print(f"Instagram: no se pudo resolver el link compartido: {e}")
        return url


def _es_highlight(url):
    return bool(re.search(r"instagram\.com/stories/highlights/\d+", url, re.I))


def _highlight_id(url):
    m = re.search(r"instagram\.com/stories/highlights/(\d+)", url, re.I)
    return m.group(1) if m else None


def _story_media_id_query(url):
    try:
        valores = parse_qs(urlparse(url).query).get("story_media_id") or []
        return str(valores[0]) if valores else None
    except Exception:
        return None


def _mejor_imagen(item):
    candidatos = ((item.get("image_versions2") or {}).get("candidates") or [])
    if not candidatos:
        return None
    mejor = max(
        candidatos,
        key=lambda x: (int(x.get("width") or 0) * int(x.get("height") or 0)),
    )
    return mejor.get("url")


def _mejor_video(item):
    versiones = item.get("video_versions") or []
    if not versiones:
        return None
    mejor = max(
        versiones,
        key=lambda x: (
            int(x.get("width") or 0) * int(x.get("height") or 0),
            int(x.get("type") or 0),
        ),
    )
    return mejor.get("url")


def descargar_highlight(url, directorio):
    """
    Descarga highlights directamente desde la API web de Instagram.
    Si el link trae story_media_id, descarga solo esa historia destacada.
    Si no, descarga todos los elementos del highlight.
    """
    highlight_id = _highlight_id(url)
    if not highlight_id:
        raise RuntimeError("No pude identificar la historia destacada")

    cookies = bot_v2.leer_cookies_instagram()
    csrf = cookies.get("csrftoken", "")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.instagram.com/",
        "Accept": "*/*",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        "X-IG-App-ID": "936619743392459",
        "X-ASBD-ID": "359341",
        "X-IG-WWW-Claim": "0",
        "X-CSRFToken": csrf,
    }

    api = "https://www.instagram.com/api/v1/feed/reels_media/"
    params = {"reel_ids": f"highlight:{highlight_id}"}

    r = requests.get(
        api,
        params=params,
        headers=headers,
        cookies=cookies,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    reel = (data.get("reels") or {}).get(f"highlight:{highlight_id}") or {}
    items = reel.get("items") or []
    if not items:
        raise RuntimeError("Instagram no devolvió contenido de la historia destacada")

    story_media_id = _story_media_id_query(url)
    if story_media_id:
        filtrados = []
        for item in items:
            pk = str(item.get("pk") or item.get("id") or "").split("_")[0]
            if pk == story_media_id:
                filtrados.append(item)
        if filtrados:
            items = filtrados
        else:
            print(
                f"Instagram highlight: no encontré story_media_id={story_media_id}; "
                "descargaré el highlight completo"
            )

    archivos = []
    for i, item in enumerate(items, start=1):
        media_type = int(item.get("media_type") or 1)
        if media_type == 2:
            media_url = _mejor_video(item)
            if not media_url:
                raise RuntimeError(f"No pude obtener el video destacado {i}")
            archivos.append(
                bot_v2.guardar_media(media_url, True, i, directorio, cookies)
            )
        else:
            media_url = _mejor_imagen(item)
            if not media_url:
                raise RuntimeError(f"No pude obtener la foto destacada {i}")
            archivos.append(
                bot_v2.guardar_media(media_url, False, i, directorio, cookies)
            )

    if not archivos:
        raise RuntimeError("La historia destacada no devolvió archivos")

    print(f"Instagram Highlight: {len(archivos)} archivo(s) descargado(s)")
    return archivos


def descargar_instagram(url, directorio):
    url_resuelta = resolver_url_instagram(url)

    if _es_highlight(url_resuelta):
        return descargar_highlight(url_resuelta, directorio)

    return _original_descargar_instagram(url_resuelta, directorio)


# Monkey-patch controlado: mantenemos intacta la lógica estable de bot_v2.
bot_v2.es_instagram = es_instagram
bot_v2.descargar_instagram = descargar_instagram

# Reexportamos lo que usa bot.py
main = bot_v2.main
iniciar_web = bot_v2.iniciar_web
