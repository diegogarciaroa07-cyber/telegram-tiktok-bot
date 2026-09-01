import os
import threading

from curl_cffi import requests as cffi_requests

import bot_v2
import bot_v5
import bot_v6


# =========================================================
# TIKTOK: TIKWM CON IMPERSONACION + YT-DLP DE RESPALDO
# =========================================================
# TikWM estaba respondiendo 403 a requests normales desde Render.
# Esta versión usa curl_cffi con impersonación de Chrome y prueba POST/GET
# en ambos hosts antes de volver a yt-dlp.

_cola_tiktok_v8 = threading.Lock()
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


def _headers_json():
    return {
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.tikwm.com/",
        "Origin": "https://www.tikwm.com",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


def _pedir_tikwm(url):
    errores = []
    hosts = ["https://www.tikwm.com/api/", "https://tikwm.com/api/"]

    for endpoint in hosts:
        try:
            print(f"TikWM: POST impersonado {endpoint}", flush=True)
            r = cffi_requests.post(
                endpoint,
                data={"url": url, "hd": "1"},
                headers=_headers_json(),
                impersonate="chrome",
                timeout=35,
                allow_redirects=True,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0 and data.get("data"):
                    return data
            errores.append(f"POST {endpoint} -> {r.status_code}")
        except Exception as e:
            errores.append(f"POST {endpoint} -> {e}")

        try:
            print(f"TikWM: GET impersonado {endpoint}", flush=True)
            r = cffi_requests.get(
                endpoint,
                params={"url": url, "hd": "1"},
                headers={
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://www.tikwm.com/",
                },
                impersonate="chrome",
                timeout=35,
                allow_redirects=True,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0 and data.get("data"):
                    return data
            errores.append(f"GET {endpoint} -> {r.status_code}")
        except Exception as e:
            errores.append(f"GET {endpoint} -> {e}")

    raise RuntimeError(" | ".join(errores))


def _descargar_archivo(url, destino):
    r = cffi_requests.get(
        url,
        headers={
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            "Referer": "https://www.tikwm.com/",
        },
        impersonate="chrome",
        timeout=90,
        allow_redirects=True,
        stream=True,
    )
    r.raise_for_status()

    with open(destino, "wb") as f:
        for bloque in r.iter_content(chunk_size=1024 * 1024):
            if bloque:
                f.write(bloque)

    if not os.path.exists(destino) or os.path.getsize(destino) == 0:
        raise RuntimeError("TikWM devolvió un archivo vacío")

    return destino


def _descargar_tikwm(url, nombre_archivo):
    print("TikTok: intentando TikWM con impersonación", flush=True)
    data = _pedir_tikwm(url)
    info = data["data"]

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
    with _cola_tiktok_v8:
        try:
            return _descargar_tikwm(url, nombre_archivo)
        except Exception as e:
            print(f"TikWM impersonado falló: {e}", flush=True)
            bot_v2.limpiar_temporales(nombre_archivo)

        print("TikTok: usando yt-dlp como respaldo", flush=True)
        return _descargar_tiktok_ytdlp(url, nombre_archivo)


bot_v2.descargar_tiktok = descargar_tiktok

# Instagram sigue en modo seguro: Reels/posts sí, Stories no.
main = bot_v5.main
iniciar_web = bot_v5.iniciar_web
