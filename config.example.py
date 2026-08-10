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

# --- Banner de abertura (thumbnail + título no topo, início de cada vídeo) ---
INTRO_BANNER = True
INTRO_SECONDS = 30
INTRO_FONT = "C:/Windows/Fonts/arialbd.ttf"  # fonte do título

# --- Publicação automática no X (ex-Twitter) ---
# Credenciais em developer.x.com (app com permissão Read and Write, OAuth 1.0a)
X_ENABLED = False
X_API_KEY = ""              # API Key (Consumer Key)
X_API_SECRET = ""           # API Key Secret
X_ACCESS_TOKEN = ""         # Access Token
X_ACCESS_TOKEN_SECRET = ""  # Access Token Secret

# Texto do post. Placeholders: {title} e {url} (link do vídeo no YouTube).
# ATENÇÃO: no plano pay-per-use do X, post com link custa ~US$0,20;
# sem link, ~US$0,015. O padrão é só o título.
X_TEXT_TEMPLATE = "{title}"

X_MAX_VIDEO_SECONDS = 140   # limite de contas comuns; Premium: use p.ex. 3600
X_IF_TOO_LONG = "trim"      # "trim" = posta cortado / "skip" = não posta
X_TARGET_SIZE_MB = 500      # acima disso, a cópia do X é recomprimida em 720p
