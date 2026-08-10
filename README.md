# Live 24/7 em loop com os vídeos do seu canal

O `loop_live.py` monitora seu canal, baixa cada vídeo novo, e transmite os últimos 20 vídeos em loop contínuo para uma live do YouTube.

## 1. Instalar no VPS Windows

Abra o PowerShell **como administrador** e rode:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp
```

Feche e reabra o PowerShell. Confirme que tudo funciona:

```powershell
python --version
ffmpeg -version
yt-dlp --version
```

Se o `winget` não estiver disponível no seu Windows Server, baixe manualmente: [Python](https://www.python.org/downloads/), [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) e [yt-dlp.exe](https://github.com/yt-dlp/yt-dlp/releases) — e coloque `ffmpeg.exe` e `yt-dlp.exe` na mesma pasta do script.

## 2. Criar a live persistente no YouTube Studio

1. YouTube Studio → **Criar** → **Transmitir ao vivo**.
2. Na aba **Transmissão** (streaming via software/RTMP), copie a **chave de transmissão**.
3. Configurações recomendadas: ative **"Ativar transmissão automática"** e desative **"Encerrar transmissão automaticamente"** (para a live sobreviver às reconexões rápidas quando entra vídeo novo). Latência: **normal**.
4. Essa chave é permanente — é ela que você cola no script.

## 3. Configurar o script

Abra o `loop_live.py` e edite o topo:

```python
CHANNEL_URL = "https://www.youtube.com/@seucanal/videos"   # mantenha o /videos no final
STREAM_KEY  = "sua-chave-aqui"
MAX_VIDEOS  = 20      # quantos vídeos ficam no loop
CHECK_INTERVAL = 300  # verifica o canal a cada 5 minutos
```

## 4. Rodar

```powershell
cd C:\caminho\da\pasta
python loop_live.py
```

Na primeira execução ele baixa e converte os 20 vídeos (pode levar um tempo, dependendo da CPU) e então começa a transmitir. Depois disso, cada vídeo novo é processado sozinho e entra no loop na próxima "virada" de vídeo.

## 5. Iniciar automático (recomendado)

Para o script sobreviver a reinicializações do servidor, use o **Agendador de Tarefas**:

1. Agendador de Tarefas → **Criar Tarefa**.
2. Geral: marque "Executar estando o usuário conectado ou não".
3. Disparadores: **Ao inicializar o sistema**.
4. Ações: programa `python`, argumentos `C:\caminho\loop_live.py`, iniciar em `C:\caminho\`.
5. Configurações: marque "Se a tarefa falhar, reiniciar a cada 1 minuto".

## Observações importantes

- **Espaço em disco**: 20 vídeos a ~4,5 Mbps ocupam na faixa de 0,5–2 GB no total (depende da duração). O script apaga automaticamente os que saem do loop.
- **Troca de vídeo novo**: quando um vídeo novo entra, há uma interrupção de ~2–5 s na transmissão. Com "encerrar automaticamente" desativado, o YouTube segura a live no ar.
- **CPU**: a transmissão em si quase não usa CPU (é cópia direta). A conversão de cada vídeo novo usa CPU por alguns minutos, 8–10× por dia.
- **Se o yt-dlp começar a falhar** com erro de "confirm you're not a bot": IPs de datacenter às vezes são bloqueados pelo YouTube. Solução: exporte os cookies do seu navegador logado no YouTube (extensão "Get cookies.txt LOCALLY") e adicione `"--cookies", "cookies.txt",` nos dois comandos `yt-dlp` do script.
- **Direitos**: os vídeos são seus, então não há problema de direitos autorais — mas se usar músicas com Content ID, a live pode receber claims normalmente.
- **Não use a aba inicial do canal** na `CHANNEL_URL` — o `/videos` garante que só uploads entram (a própria live do loop nunca é baixada).
