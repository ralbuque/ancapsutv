# -*- coding: utf-8 -*-
# Configuração da TV Ancapsu.
# Copie este arquivo para "config.py" e preencha os seus dados.
# O config.py está no .gitignore — a chave da live nunca vai para o repositório.

# URL da aba "Vídeos" do canal (mantenha o /videos no final)
CHANNEL_URL = "https://www.youtube.com/@SEU_CANAL/videos"

# Chave de transmissão da live persistente (YouTube Studio > Transmitir ao vivo)
STREAM_KEY = "xxxx-xxxx-xxxx-xxxx-xxxx"

# --- Opcionais (valores padrão mostrados; apague o que não quiser mudar) ---

MAX_VIDEOS = 20          # quantos vídeos ficam no loop
CHECK_INTERVAL = 300     # segundos entre verificações do canal

BUMPER_SOURCE_NAME = "vinheta.mp4"  # vinheta exibida entre os vídeos
CUT_END_SECONDS = 10     # segundos removidos do final de cada vídeo

BURN_SUBTITLES = True    # legendas queimadas no vídeo
WHISPER_MODEL = "small"  # "base" (rápido) / "small" (recomendado) / "medium" (lento)
SUBTITLE_STYLE = ("FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,"
                  "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
                  "Shadow=0,MarginV=30")

# Formato de saída
WIDTH, HEIGHT = 1920, 1080
FPS = 30
VIDEO_BITRATE = "4500k"
AUDIO_BITRATE = "160k"
X264_PRESET = "veryfast"
