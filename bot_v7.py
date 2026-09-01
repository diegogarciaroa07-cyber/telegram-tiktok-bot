import os
import threading

import requests

import bot_v2
import bot_v5
import bot_v6


# =========================================================
# TIKTOK: TIKWM PRIMERO + YT-DLP COMO RESPALDO
# =========================================================
# Render está recibiendo respuestas vacías/bloqueadas tanto desde la API móvil
# como desde la web de TikTok. TikWM actúa como extractor externo y solo recibe
# la URL pública del TikTok; nunca se le envían cookies ni tokens del bot.

_cola_tiktok_v7 = threading.Lock()
_descargar_tiktok_ytdlp = bot_v6.descargar_tiktok


def _normalizar_media_url(url):
    url = str(url or "").strip()
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.tikwm.com" + url
    return url


def _descargar_archivo(url, destino):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.tikwm.com/",
        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
    }

    with requests.get(url, headers=headers, stream=True, timeout=90, allow_redirects=True) as r:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloque in r.iter_content(chunk_size=1024 * 1024):
                if bloque:
                    f.write(bloque)

    if not os.path.exists(destino) or os.path.getsize(destino) == 0:
        raise RuntimeError("TikWM devolvió un archivo vacío")

    return destino


def _descargar_tikwm(url, nombre_archivo):
    print("TikTok: intentando TikWM", flush=True)

    r = requests.post(
        "https://www.tikwm.com/api/",
        data={"url": url, "hd": "1"},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.tikwm.com/",
        },
        timeout=35,
    )
    r.raise_for_status()

    data = r.json()
    if data.get("code") != 0 or not data.get("data"):
        raise RuntimeError(f"TikWM no pudo procesar el video: {data.get('msg') or 'respuesta inválida'}")

    info = data["data"]

    # TikWM puede devolver hdplay como ruta relativa y play como URL/ruta.
    # Priorizamos HD, después el video sin marca de agua y por último wmplay.
    media_url = _normalizar_media_url(
        info.get("hdplay") or info.get("play") or info.get("wmplay")
    )
    if not media_url:
        raise RuntimeError("TikWM no devolvió una URL de video")

    bot_v2.limpiar_temporales(nombre_archivo)
    _descargar_archivo(media_url, nombre_archivo)
    print("TikTok: TikWM funcionó", flush=True)
    return nombre_archivo


def descargar_tiktok(url, nombre_archivo):
    # Una sola descarga de TikTok a la vez para no saturar ni TikWM ni TikTok.
    with _cola_tiktok_v7:
        try:
            return _descargar_tikwm(url, nombre_archivo)
        except Exception as e:
            print(f"TikWM falló: {e}", flush=True)
            bot_v2.limpiar_temporales(nombre_archivo)

        # Respaldo: conserva API móvil + web/cookies de bot_v6.
        print("TikTok: usando yt-dlp como respaldo", flush=True)
        return _descargar_tiktok_ytdlp(url, nombre_archivo)


# Flask /download y Telegram toman esta función desde bot_v2.
bot_v2.descargar_tiktok = descargar_tiktok

# Instagram sigue exactamente en modo seguro: Reels/posts sí, Stories no.
main = bot_v5.main
iniciar_web = bot_v5.iniciar_web
