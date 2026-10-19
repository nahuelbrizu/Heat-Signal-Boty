# Conexión: cómo entrar al panel, a FreqUI y a la VM

> Actualizado 2026-10-19. **Acá no van claves ni contraseñas.** Se consultan con los comandos de abajo.

## Qué corre dónde
| Dónde | Qué | ¿Depende de la PC? |
|---|---|---|
| **VM** (GCP `instance-20261016-191100`, zona `asia-northeast1-a`, proyecto `vocal-operand-271906`, e2-medium 4 GB, disco 20 GB) | Ingesta, panel (:8000), grafo por sesiones, Freqtrade Donchian (:8081, FreqUI), Freqtrade de hipótesis (:8082, sin FreqUI) y los timers: bot de paper, puente, evaluación, macro, historia, archivado, respaldo y semanal | **No.** Son servicios de systemd y arrancan solos con la VM |
| **PC** (WSL Ubuntu) | Laboratorio (`~/heatsignal-lab`), timers del worker de pedidos y de `paper_compare`, y el **túnel** para mirar el panel | Sí |

Si la PC se apaga o se queda sin internet, **la VM sigue todo igual** y no se pierden datos. Lo único que se corta es
la vista del panel. Al volver, el túnel se reconecta solo.

## Acceso: túnel SSH permanente (sin puertos abiertos a internet)
El panel y FreqUI escuchan **solo en 127.0.0.1 de la VM**; no hay puertos expuestos. Se llega por un túnel SSH
desde la PC:

- Servicio de usuario en la PC: `~/.config/systemd/user/heatsignal-tunnel.service` (copia versionada en
  `deploy/pc/heatsignal-tunnel.service`).
  - Reenvía `localhost:8000` → panel y `localhost:8081` → FreqUI.
  - `ServerAliveInterval=30`: detecta un corte en ~90 s. `Restart=always` cada 10 s: se reconecta solo.
  - `ExitOnForwardFailure=yes`: si un puerto está ocupado, falla a la vista en vez de quedar a medias.
  - Habilitado (`default.target`) y con *linger* activo: arranca solo cuando arranca WSL, sin abrir sesión.
- **Windows no inicia WSL solo.** Hay que abrir una terminal de Ubuntu una vez después de prender la PC. Opcional:
  tarea de inicio de Windows con `wsl.exe -d Ubuntu --exec /bin/true` (pendiente si se quiere).

### Instalarlo en otra PC (o rehacerlo)
```bash
mkdir -p ~/.config/systemd/user
cp deploy/pc/heatsignal-tunnel.service ~/.config/systemd/user/   # ajustar la ruta de gcloud si no es /snap/bin/gcloud
systemctl --user daemon-reload
systemctl --user enable --now heatsignal-tunnel
loginctl enable-linger $USER
```
Requisito: `gcloud` instalado y logueado (`gcloud auth login`) con acceso al proyecto.

## Alias (`~/.bashrc` de la PC)
| Alias | Qué hace |
|---|---|
| `hs-open` | abre el panel → http://localhost:8000 |
| `hs-ft-open` | abre FreqUI → http://localhost:8081 |
| `hs-tunel` | estado del túnel + si responden el panel y FreqUI (código 200 = ok) |
| `hs-panel` / `hs-ft` | **reinician** el túnel permanente (ya no hace falta dejarlos abiertos en una terminal) |
| `hs-ft-pass` | muestra el usuario y la clave de FreqUI, leídos de la VM (`freqtrade/config.secrets.json`). **No pegarlos en el chat** |
| `hs-key` | carga la `ANTHROPIC_API_KEY` en la VM sin mostrarla |

## Si algo no anda
1. `hs-tunel`. Si dice `inactive` o los códigos no son 200, seguí con `hs-panel` (reinicia el túnel).
2. ¿Puerto ocupado? `ss -ltnp | grep -E ':8000|:8081'`. Si hay otro `ssh` viejo (un `gcloud compute ssh -L`
   abierto a mano), cerralo: el túnel permanente necesita los dos puertos libres.
3. Log del túnel: `journalctl --user -u heatsignal-tunnel -n 30 --no-pager`.
4. ¿La VM está prendida? `gcloud compute instances describe instance-20261016-191100 --zone asia-northeast1-a
   --project vocal-operand-271906 --format="value(status)"` → tiene que decir `RUNNING`.
5. Estado de los servicios en la VM:
   ```bash
   gcloud compute ssh --zone asia-northeast1-a --project vocal-operand-271906 instance-20261016-191100 \
     --command 'systemctl is-active heatsignal-ingestion heatsignal-freqtrade heatsignal-freqtrade-hyp; systemctl list-timers "heatsignal-*" --no-pager'
   ```

## Entrar a la VM (consola)
```bash
gcloud compute ssh --zone asia-northeast1-a --project vocal-operand-271906 instance-20261016-191100
```
- Código en `~/heatsignal` (sin git; se despliega con `git archive` + `gcloud compute scp`).
- Secretos en `~/heatsignal/.env` (permisos 600) y `~/heatsignal/freqtrade/config*.secrets.json`. Fuera de git.
- Bases en `~/heatsignal/data/`.

## Cargar o rotar una key sin pasarla por el chat
Dentro de la VM, por ejemplo para FRED:
```bash
cd ~/heatsignal && read -s -p "FRED key: " K && sed -i "/^FRED_API_KEY=/d" .env && echo "FRED_API_KEY=$K" >> .env && unset K && echo ok
sudo systemctl restart heatsignal-ingestion      # para que la tome el proceso principal
```
Igual para `GOOGLE_API_KEY` (Vertex, formato `AQ.`). Anthropic: `hs-key`.

## Seguridad
- Sin puertos públicos para el panel ni para FreqUI: todo va por SSH.
- Firewall de GCP: SSH (22) abierto. Pendientes:
  - borrar `default-allow-rdp`;
  - activar *deletion protection*;
  - más adelante, SSH solo por IAP.

  Ver `estado.md`.
- Keys que pasaron por el chat y conviene rotar: Anthropic, Gemini (AI Studio), Vertex (`AQ.`), FRED y la clave
  de FreqUI.
- Alternativas para entrar desde el celular u otra red sin abrir puertos (no implementadas): Tailscale (red
  privada) o IAP de Google. Decidirlo antes de exponer cualquier cosa.
