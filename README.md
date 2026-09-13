# Mapa mental -> Vídeo animado

Converte mapas mentais em vídeos animados (fade dos galhos um a um), feitos com
**ffmpeg/moviepy**. Interface gráfica (Tkinter) e linha de comando.

![formatos](https://img.shields.io/badge/formatos-minder%20%7C%20mm%20%7C%20txt-blue)
![python](https://img.shields.io/badge/python-3.10%2B-green)

## Formatos de entrada

| Extensão | Descrição |
|---|---|
| `.minder` | pacote do app Mind-Maps (**zip**, **gzip**, **tar.gz** ou XML puro com `map.xml`) |
| `.mm` | FreeMind (XML) |
| `.txt` / `.md` | texto indentado (2 ou 4 espaços por nível), ex.: `\t` |

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# ffmpeg precisa estar no PATH (apt install ffmpeg / winget install Gyan.FFmpeg)
```

## Como usar

### Interface gráfica

```bash
python app.py          # sem argumentos abre a janela
```

Clique em **Carregar mapa** (`.minder`/`.mm`) ou **Texto indentado**, escolha
tema, ritmo (tempo por nó, fade, pausa final), resolução, FPS e formato
(MP4/WebM), depois **Gerar vídeo**.

### Linha de comando

```bash
python app.py mapa.minder -o video.mp4                    # 1280x720 @ 30fps
python app.py mapa.mm -o video.webm --tema dark           # WebM (VP9)
python app.py notes.txt --largura 854 --altura 480 --fps 24 \
    --tempo-por-no 0.3 --pausa-final 2 --fade 0.4
python app.py --listar-resolucoes
```

Parâmetros:

| Opção | Padrão | Descrição |
|---|---|---|
| `--tema` | arquivo/`default` | `default`, `dark`, `solarized-dark`, `solarized-light` |
| `--tempo-por-no` | `0.45` | segundos entre a entrada de cada nó |
| `--fade` | `0.35` | duração da transição de cada nó |
| `--pausa-final` | `2.0` | segundos mostrando o mapa completo |
| `--largura` / `--altura` | `1280` / `720` | resolução |
| `--fps` | `30` | quadros por segundo (24/30/60) |

## Recursos

- Temas de cores (inclusive o tema "dark" embutido no arquivo `.minder`)
- Emojis coloridos (fonte Noto Color Emoji)
- Rendering estilo Minder: raiz central, galhos em curvas cúbicas, boxes
  arredondados, espaçamento adaptativo à largura dos textos
- Pré-estima do tempo e barra de progresso

## Estrutura

- `app.py` — parsers, layout, renderização PIL, codificação moviepy, CLI e GUI
- `requirements.txt` — `moviepy`, `pillow`, `numpy`