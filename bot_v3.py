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
    """Decodifica enlaces /s/ que contienen un highlight:<id>."""
    try:
        parsed = urlparse(str(url or "").strip())
        m = re.search(r"/s/([^/?#]+)", parsed.path, re.I)
        if not m:
            return None

        token = m.group(1)
        token += "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token.encode()).decode("utf-8", errors="ignore")
        print(f"Instagram: token /s/ decodificado como {decoded}", flush=True)

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
        print(f"Instagram: no se pudo decodificar /s/: {e}", flush=True)
        return None


def resolver_url_instagram(url):
    """Convierte enlaces compartidos de Instagram a una URL utilizable."""
    url = str(url or "").strip()
    lower = url.lower()

    if "instagram.com/s/" in lower:
        decodificada = _decodificar_link_s(url)
        if decodificada:
            print(f"Instagram: /s/ decodificado a {decodificada}", flush=True)
            return decodificada

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

        if "instagram.com/s/" in final_url.lower():
            decodificada = _decodificar_link_s(final_url)
            if decodificada:
                final_url = decodificada

        print(f"Instagram: link resuelto a {final_url}", flush=True)
        return final_url
    except Exception as e:
        print(f"Instagram: no se pudo resolver el link compartido: {e}", flush=True)
        return url


def _es_highlight(url):
    return bool(re.search(r"instagram\.com/stories/highlights/\d+", str(url or ""), re.I))


def _highlight_id(url):
    m = re.search(r"instagram\.com/stories/highlights/(\d+)", str(url or ""), re.I)
    return m.group(1) if m else None


def _story_media_id_query(url):
    try:
        valores = parse_qs(urlparse(url).query).get("story_media_id") or []
        return str(valores[0]).split("_")[0] if valores else None
    except Exception:
        return None


def _mejor_imagen(item):
    # Respuesta de la API móvil de Instagram
    candidatos = ((item.get("image_versions2") or {}).get("candidates") or [])
    if candidatos:
        mejor = max(
            candidatos,
            key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0),
        )
        if mejor.get("url"):
            return mejor["url"]

    # Respuesta GraphQL de Instaloader
    recursos = item.get("display_resources") or []
    if recursos:
        mejor = max(
            recursos,
            key=lambda x: int(x.get("config_width") or x.get("width") or 0)
            * int(x.get("config_height") or x.get("height") or 0),
        )
        if mejor.get("src"):
            return mejor["src"]

    return item.get("display_url") or item.get("display_src")


def _mejor_video(item):
    # Respuesta de la API móvil de Instagram
    versiones = item.get("video_versions") or []
    if versiones:
        mejor = max(
            versiones,
            key=lambda x: (
                int(x.get("width") or 0) * int(x.get("height") or 0),
                int(x.get("type") or 0),
            ),
        )
        if mejor.get("url"):
            return mejor["url"]

    # Respuesta GraphQL de Instaloader
    recursos = item.get("video_resources") or []
    if recursos:
        mejor = max(
            recursos,
            key=lambda x: int(x.get("config_width") or x.get("width") or 0)
            * int(x.get("config_height") or x.get("height") or 0),
        )
        if mejor.get("src"):
            return mejor["src"]

    return item.get("video_url")


def _item_es_video(item):
    if "is_video" in item:
        return bool(item.get("is_video"))
    try:
        return int(item.get("media_type") or 1) == 2
    except Exception:
        return str(item.get("__typename") or "").lower().endswith("video")


def _id_item(item):
    return str(item.get("pk") or item.get("id") or "").split("_")[0]


def _obtener_items_highlight(highlight_id):
    """
    Usa la propia sesión HTTP de Instaloader. Esto es importante porque
    Instagram exige cabeceras/dispositivo de sesión además de las cookies.
    """
    loader = None
    try:
        loader, cookies = bot_v2.crear_instaloader()
        clave = f"highlight:{highlight_id}"

        # Método principal: endpoint iPhone de Instaloader.
        try:
            data = loader.context.get_iphone_json(
                path=f"api/v1/feed/reels_media/?reel_ids={clave}",
                params={},
            )
            reel = (data.get("reels") or {}).get(clave) or {}
            items = reel.get("items") or []
            print(
                f"Instagram Highlight: API móvil devolvió {len(items)} elemento(s)",
                flush=True,
            )
            if items:
                return items, cookies
        except Exception as e:
            print(f"Instagram Highlight: API móvil falló: {e}", flush=True)

        # Fallback: misma consulta GraphQL que usa Highlight._fetch_items().
        try:
            data = loader.context.graphql_query(
                "45246d3fe16ccc6577e0bd297a5db1ab",
                {
                    "reel_ids": [],
                    "tag_names": [],
                    "location_ids": [],
                    "highlight_reel_ids": [str(highlight_id)],
                    "precomposed_overlay": False,
                },
            )
            reels_media = ((data.get("data") or {}).get("reels_media") or [])
            items = (reels_media[0].get("items") or []) if reels_media else []
            print(
                f"Instagram Highlight: GraphQL devolvió {len(items)} elemento(s)",
                flush=True,
            )
            if items:
                return items, cookies
        except Exception as e:
            print(f"Instagram Highlight: GraphQL falló: {e}", flush=True)

        raise RuntimeError("Instagram no devolvió contenido de la historia destacada")
    finally:
        if loader is not None:
            try:
                loader.close()
            except Exception:
                pass


def descargar_highlight(url, directorio):
    highlight_id = _highlight_id(url)
    if not highlight_id:
        raise RuntimeError("No pude identificar la historia destacada")

    print(f"Instagram Highlight: id={highlight_id}", flush=True)
    items, cookies = _obtener_items_highlight(highlight_id)

    story_media_id = _story_media_id_query(url)
    if story_media_id:
        filtrados = [item for item in items if _id_item(item) == story_media_id]
        if filtrados:
            items = filtrados
            print(
                f"Instagram Highlight: seleccionado story_media_id={story_media_id}",
                flush=True,
            )
        else:
            print(
                f"Instagram Highlight: story_media_id={story_media_id} no apareció; "
                "se enviará el highlight completo",
                flush=True,
            )

    archivos = []
    for i, item in enumerate(items, start=1):
        if _item_es_video(item):
            media_url = _mejor_video(item)
            if not media_url:
                raise RuntimeError(f"No pude obtener el video destacado {i}")
            archivos.append(bot_v2.guardar_media(media_url, True, i, directorio, cookies))
        else:
            media_url = _mejor_imagen(item)
            if not media_url:
                raise RuntimeError(f"No pude obtener la foto destacada {i}")
            archivos.append(bot_v2.guardar_media(media_url, False, i, directorio, cookies))

    if not archivos:
        raise RuntimeError("La historia destacada no devolvió archivos")

    print(f"Instagram Highlight: {len(archivos)} archivo(s) descargado(s)", flush=True)
    return archivos


def descargar_instagram(url, directorio):
    url_resuelta = resolver_url_instagram(url)
    print(f"Instagram: URL normalizada = {url_resuelta}", flush=True)

    if _es_highlight(url_resuelta):
        return descargar_highlight(url_resuelta, directorio)

    return _original_descargar_instagram(url_resuelta, directorio)


# Monkey-patch controlado: mantenemos intacta la lógica estable de bot_v2.
bot_v2.es_instagram = es_instagram
bot_v2.descargar_instagram = descargar_instagram

# Reexportamos lo que usa bot.py
main = bot_v2.main
iniciar_web = bot_v2.iniciar_web
