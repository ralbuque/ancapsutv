# Live 24/7 em loop com os vídeos do seu canal

O `loop_live.py` monitora seu canal, baixa cada vídeo novo, corta os 10 segundos finais (fechamento), gera legendas automaticamente e as queima no vídeo, e transmite os últimos 20 vídeos em loop contínuo para uma live do YouTube — com a vinheta "essa é a TV ancapsu" entre cada vídeo.

## 1. Instalar no VPS Windows

Abra o PowerShell **como administrador** e rode:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp
```

Depois instale o transcritor de legendas:

```powershell
pip install faster-whisper
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

## 3. Configurar

Copie o `config.example.py` para `config.py` e preencha:

```powershell
copy config.example.py config.py
```

```python
CHANNEL_URL = "https://www.youtube.com/@seucanal/videos"   # mantenha o /videos no final
STREAM_KEY  = "sua-chave-aqui"
```

Todo o resto (número de vídeos, corte final, modelo de legenda, resolução etc.) tem valores padrão e também pode ser ajustado no `config.py`. O arquivo está no `.gitignore`, então a chave da live nunca vai para o repositório — pode versionar e publicar o resto sem preocupação.

**Vinheta:** coloque o seu vídeo "essa é a TV ancapsu" na mesma pasta do script com o nome **`vinheta.mp4`**. Ele será convertido automaticamente e exibido entre todos os vídeos do loop. Se você trocar o arquivo depois, o script detecta e atualiza sozinho.

**Legendas:** cada vídeo é transcrito localmente (Whisper) e a legenda é gravada por cima da imagem — não depende das legendas do YouTube. Escolha do modelo no `WHISPER_MODEL`:

| Modelo | Qualidade (pt-BR) | Velocidade em CPU |
|---|---|---|
| `base` | razoável | rápido (~1× a duração do vídeo) |
| `small` | boa (recomendado) | médio (~2–3×) |
| `medium` | ótima | lento (~6–8×) |

Na primeira execução o modelo é baixado (~500 MB para o `small`). Se a transcrição estiver atrasando a entrada dos vídeos no loop, troque para `base`. Estilo da legenda (fonte, tamanho, posição) é ajustável em `SUBTITLE_STYLE` no script.

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

## Publicação automática no X (opcional)

Com `X_ENABLED = True` no `config.py`, cada vídeo novo processado (já cortado e legendado) é postado no X com o título do vídeo. Requer `python -m pip install tweepy`.

Para obter as credenciais:

1. Acesse [developer.x.com](https://developer.x.com) logado com a conta do X que vai postar e crie uma conta de desenvolvedor. Novas contas usam o plano **pay-per-use**: é preciso cadastrar pagamento e comprar créditos (~US$ 0,015 por post com vídeo; 8–10 posts/dia ≈ US$ 4–5/mês).
2. Crie um **Project** e dentro dele um **App**.
3. No App: **User authentication settings** → habilite **OAuth 1.0a** com permissão **Read and Write** (as URLs de callback/website podem ser a do seu canal — não são usadas).
4. Em **Keys and tokens**, copie: **API Key**, **API Key Secret**, e gere o **Access Token e Secret** (confira que aparecem como "Read and Write" — se gerou antes de mudar a permissão, regenere).
5. Cole os quatro valores no `config.py`.

Detalhes: contas comuns só aceitam vídeos de até 2min20s — o script corta o post nesse limite por padrão (`X_IF_TOO_LONG = "trim"`; use `"skip"` para não postar os longos, ou aumente `X_MAX_VIDEO_SECONDS` se a conta tiver Premium). Evite `{url}` no `X_TEXT_TEMPLATE`: post com link custa ~US$ 0,20 em vez de US$ 0,015.

**Importante:** ative o X só depois da carga inicial dos 20 vídeos terminar, senão o script posta o lote antigo inteiro de uma vez.

## Observações importantes

- **Espaço em disco**: 20 vídeos a ~4,5 Mbps ocupam na faixa de 0,5–2 GB no total (depende da duração). O script apaga automaticamente os que saem do loop.
- **Troca de vídeo novo**: quando um vídeo novo entra, há uma interrupção de ~2–5 s na transmissão. Com "encerrar automaticamente" desativado, o YouTube segura a live no ar.
- **CPU**: a transmissão em si quase não usa CPU (é cópia direta). O processamento de cada vídeo novo (transcrição + conversão) usa CPU por alguns minutos, 8–10× por dia — o vídeo entra no loop assim que fica pronto.
- **Vídeos muito curtos** (menos de ~15 s) não têm o final cortado, para não sumirem.
- **Vinheta**: só o `vinheta.mp4` original precisa estar na pasta — o `bumper.ts` (versão convertida) é gerado automaticamente.
- **Se o yt-dlp começar a falhar** com erro de "confirm you're not a bot": IPs de datacenter às vezes são bloqueados pelo YouTube. Solução: exporte os cookies do seu navegador logado no YouTube (extensão "Get cookies.txt LOCALLY") e adicione `"--cookies", "cookies.txt",` nos dois comandos `yt-dlp` do script.
- **Direitos**: os vídeos são seus, então não há problema de direitos autorais — mas se usar músicas com Content ID, a live pode receber claims normalmente.
- **Não use a aba inicial do canal** na `CHANNEL_URL` — o `/videos` garante que só uploads entram (a própria live do loop nunca é baixada).
