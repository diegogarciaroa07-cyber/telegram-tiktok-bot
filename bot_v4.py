import os
import requests

import bot_v2
import bot_v3


# =========================================================
# STORIES NORMALES: DESCARGA DIRECTA POR MEDIA ID
# =========================================================


def _headers_instagram_mobile(cookies):
    headers = {
        "User-Agent": (
            "Instagram 361.0.0.35.82 (iPad13,8; iOS 18_0; en_US; en-US; "
            "scale=2.00; 2048x2732; 674117118) AppleWebKit/420+"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "x-ig-app-id": "124024574287414",
        "x-ig-capabilities": "36r/F/8=",
        "x-ig-connection-type": "WiFi",
        "x-ig-app-locale": "en-US",
        "x-ig-device-locale": "en-US",
        "x-ig-www-claim": "0",
        "Referer": "https://www.instagram.com/",
    }
    csrf = (cookies or {}).get("csrftoken")
    if csrf:
        headers["X-CSRFToken"] = csrf
    return headers


def _obtener_story_por_media_id(media_id):
    """
    Obtiene directamente la Story exacta desde /media/<id>/info/.
    Así evitamos web_profile_info y la GraphQL antigua, que estaban
    provocando feedback_required y 429 con esperas muy largas.
    """
    cookies = bot_v2.leer_cookies_instagram()
    headers = _headers_instagram_mobile(cookies)

    endpoints = (
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
    )

    ultimo_error = None

    for endpoint in endpoints:
        try:
            r = requests.get(
                endpoint,
                headers=headers,
                cookies=cookies,
                timeout=20,
                allow_redirects=True,
            )

            print(
                f"Instagram Story directa: {endpoint} -> HTTP {r.status_code}",
                flush=True,
            )

            if r.status_code == 429:
                raise RuntimeError(
                    "Instagram está limitando temporalmente las solicitudes; inténtalo de nuevo en unos minutos"
                )

            if r.status_code in (401, 403):
                raise RuntimeError(
                    "Instagram rechazó la sesión para esta historia; puede que las cookies hayan caducado"
                )

            if r.status_code == 404:
                ultimo_error = RuntimeError(
                    "La historia ya no está disponible o fue eliminada"
                )
                continue

            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []

            if not items:
                ultimo_error = RuntimeError(
                    "Instagram no devolvió datos para esa historia"
                )
                continue

            objetivo = None
            for item in items:
                if bot_v3._id_item(item) == str(media_id):
                    objetivo = item
                    break

            if objetivo is None:
                objetivo = items[0]

            print(
                f"Instagram Story directa: encontrada media_id={bot_v3._id_item(objetivo)}",
                flush=True,
            )
            return objetivo, cookies

        except RuntimeError:
            raise
        except Exception as e:
            ultimo_error = e
            print(f"Instagram Story directa: endpoint falló: {e}", flush=True)

    raise ultimo_error or RuntimeError("No pude obtener la historia de Instagram")


def descargar_story_normal(url, directorio):
    username, media_id = bot_v3._story_normal_partes(url)
    if not username or not media_id:
        raise RuntimeError("No pude identificar la historia")

    os.makedirs(directorio, exist_ok=True)
    item, cookies = _obtener_story_por_media_id(media_id)

    if bot_v3._item_es_video(item):
        media_url = bot_v3._mejor_video(item)
        if not media_url:
            raise RuntimeError("Instagram no devolvió el video de la historia")
        archivo = bot_v2.guardar_media(media_url, True, 1, directorio, cookies)
        print("Instagram Story directa: video descargado", flush=True)
    else:
        media_url = bot_v3._mejor_imagen(item)
        if not media_url:
            raise RuntimeError("Instagram no devolvió la foto de la historia")
        archivo = bot_v2.guardar_media(media_url, False, 1, directorio, cookies)
        print("Instagram Story directa: foto descargada", flush=True)

    return [archivo]


def descargar_instagram(url, directorio):
    url_resuelta = bot_v3.resolver_url_instagram(url)
    print(f"Instagram: URL normalizada = {url_resuelta}", flush=True)

    # Destacadas quedan exactamente con la lógica anterior por ahora.
    if bot_v3._es_highlight(url_resuelta):
        return bot_v3.descargar_highlight(url_resuelta, directorio)

    # Solo reemplazamos el flujo de Stories normales.
    if bot_v3._es_story_normal(url_resuelta):
        return descargar_story_normal(url_resuelta, directorio)

    # Posts/Reels/fotos siguen usando la lógica existente.
    return bot_v3._original_descargar_instagram(url_resuelta, directorio)


bot_v2.es_instagram = bot_v3.es_instagram
bot_v2.descargar_instagram = descargar_instagram

main = bot_v2.main
iniciar_web = bot_v2.iniciar_web
