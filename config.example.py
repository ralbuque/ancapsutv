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
WHISPER_THREADS = 2      # núcleos usados na transcrição (deixe folga p/ a live)
SUBTITLE_STYLE = ("FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,"
                  "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
                  "Shadow=0,MarginV=60")  # 60 ≈ 225px do rodapé (folga p/ lower third)

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

# --- Lower third (nome + canal + barra de títulos na base da tela) ---
# ATENÇÃO: liga a re-codificação contínua da transmissão (usa bastante CPU).
# Se pesar no servidor, volte para False e reinicie — o modo leve retorna.
LOWER_THIRD = False
LOWER_THIRD_NAME = "Peter Turguniev"
LOWER_THIRD_CHANNEL = "ANCAPSU"
LOWER_THIRD_PRESET = "veryfast"  # se a CPU sofrer, tente "superfast"
TICKER_COUNT = 3      # quantos títulos recentes ciclam na barra
TICKER_SECONDS = 8    # segundos que cada título fica na tela
ALERT_MINUTES = 10    # duração do aviso de vídeo novo
LOWER_THIRD_BADGE = "badge.png"  # PNG transparente com o emblema (nome+canal);
                                 # se existir, substitui os textos da esquerda
TICKER_LEAD_SPACES = 56          # espaços antes do texto (para sair do emblema)

# Barra "AGORA" (título do item em exibição) e relógio
AGORA_ENABLED = True
AGORA_SHORT_TEMPLATE = "Short do palácio assombrado {title}"
LT_CLOCK = True
CLOCK_UTC_OFFSET = -3            # Brasília (independe do fuso do servidor)
CLOCK_DATE_X = 190               # posições dentro do emblema (ajuste ao PNG)
CLOCK_DATE_Y = "h-110"
CLOCK_TIME_X = 190
CLOCK_TIME_Y = "h-64"
AGORA_LEAD_SPACES = 64           # recuo do texto AGORA (fonte menor = mais espaços)
ALERT_PREFIX = "LANÇADO: "       # prefixo do aviso de vídeo novo
TICKER_BOX = "0xF2B705"          # cor da barra normal (dourada)
ALERT_BOX = "0xEF7B6D"           # cor da barra de alerta (salmão)

# --- Bloco promocional do outro canal (alterna com a vinheta) ---
# Sequência: chamada.mp4 -> um short do outro canal -> continuidade.mp4
PROMO_ENABLED = False
PROMO_CHANNEL_URL = "https://www.youtube.com/@SEU_OUTRO_CANAL/shorts"
PROMO_INTRO_NAME = "chamada.mp4"        # "Conheça nosso projeto..."
PROMO_OUTRO_NAME = "continuidade.mp4"   # "Continue agora com a TV ANCAPSU"
PROMO_MAX_SHORTS = 10  # rodízio com os N shorts mais recentes
PROMO_EVERY = 2        # 2 = alterna vinheta/promo; 3 = promo a cada 3 intervalos

# --- Instagram/TikTok via Ayrshare (versão vertical automática) ---
# Conecte as contas no painel da Ayrshare e cole a API Key aqui.
AYRSHARE_ENABLED = False
AYRSHARE_API_KEY = ""
AYR_PLATFORMS = ["instagram", "tiktok"]
AYR_CAPTION_TEMPLATE = "{title}"
AYR_COMMENT_TEMPLATE = "Veja o vídeo completo em {url}"
AYR_MAX_SECONDS = 1190   # corte da versão vertical (Reels aceita até 20 min;
                         # OBS: Reels >3 min não são recomendados a não-seguidores)
VERT_BG = "0xD35A25"     # cor das faixas superior/inferior
VERT_CHANNEL_TEXT = "Canal ANCAPSU"

# --- Canais convidados (1 vídeo de cada no ciclo, em posição fixa) ---
# Coloque as vinhetas .mp4 de cada canal na pasta do script.
GUEST_CHANNELS = [
    # {
    #     "name": "Mundo em Revolução",
    #     "url": "https://www.youtube.com/@canal/videos",  # aba /videos
    #     "position": 10,               # posição no ciclo (1-based)
    #     "intro": "mundo_intro.mp4",   # vinheta apresentando o canal
    #     "outro": "mundo_outro.mp4",   # vinheta de volta à programação
    #     "x_api_key": "",              # credenciais da conta X DESSE canal
    #     "x_api_secret": "",
    #     "x_access_token": "",
    #     "x_access_token_secret": "",
    #     # "cut_end": 10,  # segundos cortados do final (0 = não cortar)
    # },
    # {
    #     "name": "Safesrc",
    #     "url": "https://www.youtube.com/@canal2/videos",
    #     "position": 16,
    #     "intro": "safesrc_intro.mp4",
    #     "outro": "safesrc_outro.mp4",
    #     "x_api_key": "", "x_api_secret": "",
    #     "x_access_token": "", "x_access_token_secret": "",
    # },
]

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
X_ENDCARD = True            # em posts cortados, os 10s finais viram a tela
                            # "Veja o vídeo completo no YouTube" com thumb+link

# Reply automático ao post com o link do YouTube (em TODO post, cortado ou não).
# Custo: reply com link ≈ US$0,20 cada no pay-per-use (~US$60/mês com 10/dia).
# Deixe "" para desativar.
X_REPLY_TEMPLATE = "Veja o vídeo completo em {url}"
