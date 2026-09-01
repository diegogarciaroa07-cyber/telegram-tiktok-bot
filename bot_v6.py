import os
import threading

import yt_dlp

import bot_v2
import bot_v5


# =========================================================
# TIKTOK: API PRIMERO + UNA DESCARGA A LA VEZ
# =========================================================
# Mantiene intacto el modo seguro temporal de Instagram de bot_v5.
# El objetivo es evitar varias descargas simultáneas y saltar el webpage
# challenge de TikTok cuando sea posible usando la API móvil de yt-dlp.

_descargar_tiktok_anterior = bot_v2.descargar_tiktok
_cola_tiktok_global = threading.Lock()


def _opciones_tiktok_api(nombre_archivo):
    iid = os.getenv("TIKTOK_IID")
    if iid:
        app_info = [
            f"{iid}/musical_ly/35.1.3/2023501030/1233",
            f"{iid}/trill/35.1.3/2023501030/1180",
        ]
    else:
        # Deja el IID vacío pero fuerza a yt-dlp a intentar primero la API móvil.
        app_info = [
            "/musical_ly/35.1.3/2023501030/1233",
            "/trill/35.1.3/2023501030/1180",
        ]

    opciones = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "extractor_args": {
            "tiktok": {
                "app_info": app_info,
            }
        },
    }

    if os.path.exists(bot_v2.TIKTOK_COOKIEFILE):
        opciones["cookiefile"] = bot_v2.TIKTOK_COOKIEFILE

    return opciones


def descargar_tiktok(url, nombre_archivo):
    # Protege tanto Shortcut/Flask como Telegram para que nunca golpeen TikTok
    # en paralelo desde la misma instancia de Render.
    with _cola_tiktok_global:
        bot_v2.limpiar_temporales(nombre_archivo)

        try:
            print("TikTok: intentando API móvil primero", flush=True)
            with yt_dlp.YoutubeDL(_opciones_tiktok_api(nombre_archivo)) as ydl:
                ydl.download([url])

            if not os.path.exists(nombre_archivo):
                raise RuntimeError("yt-dlp API terminó pero no creó el archivo")

            print("TikTok: API móvil funcionó", flush=True)
            return nombre_archivo

        except Exception as e:
            print(f"TikTok API móvil falló: {e}", flush=True)
            bot_v2.limpiar_temporales(nombre_archivo)

        # Si la API móvil falla, conservamos exactamente los métodos web/cookies
        # que ya tenía la versión anterior.
        print("TikTok: usando respaldo web anterior", flush=True)
        return _descargar_tiktok_anterior(url, nombre_archivo)


# El endpoint /download y Telegram consultan este símbolo en bot_v2 en tiempo de ejecución.
bot_v2.descargar_tiktok = descargar_tiktok

# Instagram permanece exactamente en el modo seguro de bot_v5.
main = bot_v5.main
iniciar_web = bot_v5.iniciar_web
