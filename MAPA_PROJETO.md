# 🗺️ Mapa do Projeto — BlackLive Local Relay

---

## 🔖 COMMIT DE REFERÊNCIA — ANTES DO RELAY

> Se precisar voltar ao estado anterior ao relay, use este commit:

```
git checkout d69e640
```

| | |
|---|---|
| **Hash** | `d69e640` |
| **Tag** | `v1.0-blacklive-estudio` |
| **Mensagem** | CHECKPOINT v1.0 - Studio funcional: mic+monitor, handles responsivos, camera iOS, volume player, relay local |
| **Data** | 14/06/2026 |
| **O que funciona** | Tudo em produção em blacklive.com.br |

```bash
# Para voltar ao estado funcional sem o relay:
git checkout d69e640

# Para voltar à versão mais recente:
git checkout main
```

---
## Executável para usar o IP do cliente (residencial/4G)

---

## ✅ Conceito Principal

**O executável é LEVE — a live roda toda no browser.**

```
┌─────────────────────────────────────────────────────────┐
│                    BROWSER DO USUÁRIO                    │
│                                                          │
│  https://blacklive.com.br/sala/.../fabrica_blocos...     │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Canvas (camadas, texto, imagem, câmera, clock)  │   │
│  │  IA (blocos de venda) ← VPS processa             │   │
│  │  TTS (voz) ← VPS processa                       │   │
│  │  Áudio ambiente ← VPS processa                   │   │
│  └──────────────────────────────────────────────────┘   │
│                         │                                │
│              MediaRecorder → WebM                        │
└─────────────────────────┼───────────────────────────────┘
                          │
                          ▼
          ws://localhost:8902  ← EXECUTÁVEL (leve)
                          │
                 FFmpeg (bundled)
                          │
                          ▼
              RTMP → TikTok
          (IP RESIDENCIAL DO USUÁRIO) ✅
```

---

## 🎯 O que o executável FAZ

| Função | Faz? |
|---|---|
| Renderizar canvas / camadas | ❌ (browser faz) |
| Gerar IA / blocos de venda | ❌ (VPS faz) |
| TTS / voz | ❌ (VPS faz) |
| Receber WebM do browser | ✅ |
| Transcodar VP8 → H.264 (FFmpeg) | ✅ |
| Enviar RTMP para o TikTok | ✅ |
| Usar IP residencial do usuário | ✅ |

---

## 🪶 Por que é leve

O executável não renderiza vídeo nem processa IA.
Ele só faz: **receber → transcodar → enviar**.

| Componente | RAM em uso |
|---|---|
| Python runtime | ~30 MB |
| Servidor WebSocket | ~5 MB |
| FFmpeg (durante live) | ~60 MB |
| Ícone bandeja (pystray) | ~5 MB |
| **Total** | **~100 MB** |

**Comparação:**
- TikTok Live Studio: 500 MB – 1 GB RAM, 20-40% CPU
- BlackLive Relay: ~100 MB RAM, 5-15% CPU

---

## 📁 Estrutura da Pasta

```
relay_local/
├── MAPA_PROJETO.md      ← Este arquivo
├── local_relay.py       ← Lógica WebSocket → RTMP
├── relay_tray.py        ← Ícone na bandeja + auto-start
├── build.py             ← Gera .exe/.app automaticamente
├── relay.spec           ← Config PyInstaller
└── icon.png             ← Ícone BlackLive (bandeja)
```

---

## ⚙️ Tecnologias usadas

| Biblioteca | Função | Tamanho |
|---|---|---|
| `websockets` | Servidor WebSocket local | ~1 MB |
| `imageio-ffmpeg` | FFmpeg bundled (sem instalar) | ~80 MB |
| `pystray` | Ícone na bandeja (invisível) | ~2 MB |
| `Pillow` | Ícone PNG para pystray | ~5 MB |
| `PyInstaller` | Gera .exe/.app | build only |

---

## 🔄 Fluxo de Comunicação

```
1. Usuário instala o executável (1x só)
2. Executável inicia → ícone aparece na bandeja
3. Usuário clica no ícone → browser abre o studio
4. Studio detecta ws://localhost:8902 automaticamente
5. Mostra: "🟢 IP Local (residencial)"
6. Usuário clica "Ligar Transmissão" normalmente
7. Stream vai: Browser → Relay local → TikTok
   (usando IP residencial, não IP do VPS)
```

---

## 🚫 O que NÃO muda no projeto atual

- `server.py` → NÃO mexe
- `rtmp_streamer.py` → NÃO mexe
- `fabrica_blocos_final.html` → NÃO mexe (até integração)
- `estudio_beta.js` → NÃO mexe (até integração)

---

## 📋 Plano de Implementação

| Passo | Arquivo | Status |
|---|---|---|
| 1. Criar pasta relay_local | — | ✅ Feito |
| 2. Anotar mapa do projeto | MAPA_PROJETO.md | ✅ Feito |
| 3. Refatorar local_relay.py com imageio-ffmpeg | local_relay.py | ⬜ |
| 4. Criar relay_tray.py (bandeja + auto-start) | relay_tray.py | ⬜ |
| 5. Criar ícone BlackLive | icon.png | ⬜ |
| 6. Criar build.py (PyInstaller automatizado) | build.py | ⬜ |
| 7. Testar localmente (python relay_tray.py) | — | ⬜ |
| 8. Gerar .exe Windows | dist/BlackLive-Relay-Win.exe | ⬜ |
| 9. Gerar .app Mac | dist/BlackLive-Relay-Mac.app | ⬜ |
| 10. Integrar detecção no studio | estudio_beta.js | ⬜ |

---

## 💰 Modelo de Planos (futuro)

| Plano | RTMP via | IP | Instala? |
|---|---|---|---|
| Starter | Relay local (PC) | Residencial ✅ | Sim (1x) |
| Cloud | VPS | Datacenter ⚠️ | Não |
| Internacional | VPS + Proxy | País do proxy | Não |

---

## 📅 Criado em: 14/06/2026
## 🔖 Versão: 1.0-beta
