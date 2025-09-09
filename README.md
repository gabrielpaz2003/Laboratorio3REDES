# 🛰️ Laboratorio 3 – Algoritmos de Enrutamiento (LSR, Dijkstra & Flooding)

> **Proyecto base:** red de nodos asíncronos que se comunican por **Redis Pub/Sub** y construyen tablas de ruteo usando **Link State Routing (LSR)** con **Dijkstra** (y **flooding** como fallback controlado).  
> **Objetivo:** levantar varios nodos (procesos) en tu máquina, que descubren vecinos, comparten estado, calculan rutas, y **envían mensajes extremo a extremo**.

---

## ✨ Características

- ✅ **Protocolos y tipos de paquetes** (`proto`, `type`, `from`, `to`, `ttl`, `headers`, `payload`) en JSON.
- ✅ **HELLO** periódico para detectar vecinos/caídas.
- ✅ **INFO (LSP/LSDb)** para reconstruir la topología y recalcular rutas con **Dijkstra**.
- ✅ **MESSAGE** unicast con **tabla de ruteo** (o **flooding controlado** si no hay ruta).
- ✅ **De-dupe** por `msg_id`, **anti-ciclo** por `headers` y **TTL** decreciente.
- ✅ **Persistencia local** de vecinos, LSDB y routing table.
- ✅ **Consola coloreada** y tablas bonitas de rutas (Rich).
- ✅ Configurable vía **.env** por nodo (A, B, C, D, E).

> ⚠️ Fase XMPP: el laboratorio oficial pide probar con XMPP. Esta base usa Redis para desarrollo local y **está lista para integrar un transporte XMPP** (ver sección _XMPP_ abajo para lineamientos).

---

## 📁 Estructura del proyecto

```
Lab3-Redes/
├─ configs/
│  ├─ names.json        # Mapea ID → canal (Redis topic)
│  └─ topo.json         # Vecinos iniciales por nodo
├─ env/                 # Archivos .env por nodo
│  ├─ A.env  B.env  C.env  D.env  E.env
├─ src/
│  ├─ main.py           # CLI de arranque/envío
│  ├─ node.py           # Orquestador del nodo
│  ├─ services/
│  │  ├─ fowarding.py   # Forwarding (HELLO/INFO/MESSAGE)
│  │  ├─ routing_lsr.py # LSDB + Dijkstra + anuncios INFO
│  ├─ protocol/
│  │  ├─ schema.py      # Pydantic models de paquetes
│  │  └─ builders.py    # Helpers para construir paquetes
│  ├─ storage/
│  │  └─ state.py       # Vecinos, LSDB, routing_table, caches
│  ├─ transport/
│  │  └─ redis_transport.py # Pub/Sub asíncrono
│  └─ utils/
│     └─ log.py         # Logger con Rich
├─ requirements.txt
└─ README.md
```

---

## 🔧 Requisitos

- **Python 3.10+** (recomendado 3.11)
- **Redis** (local o remoto)
- Windows (PowerShell) / macOS / Linux

Paquetes principales:
```
redis[hiredis]>=5.0.4
pydantic>=2.7.0
anyio>=4.4.0
typer>=0.12.3
python-dotenv>=1.0.1
rich>=13.7.1
```
> Instalan todos con `pip install -r requirements.txt`

---

## 🚀 Instalación rápida

### 1) Crear y activar venv

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2) Redis: local o remoto

- **Local:** instalar Redis y levantar en `localhost:6379` (sin password).
- **Remoto (ejemplo):**
  - Host: `homelab.fortiguate.com`
  - Puerto: `16379`
  - Password: `4YNydkHFPcayvlx7$zpKm`  
  *(⚠️ Cambia estos valores en tus .env; no los subas al repo público).*
  
### 3) Configuración JSON (vecinos y canales)

`configs/names.json`
```json
{
  "type": "names",
  "config": {
    "A": "sec30.topologia1.node1",
    "B": "sec30.topologia1.node2",
    "C": "sec30.topologia1.node3",
    "D": "sec30.topologia1.node4",
    "E": "sec30.topologia1.node5"
  }
}
```

`configs/topo.json`
```json
{
  "type": "topo",
  "config": {
    "A": ["B"],
    "B": ["C", "D"],
    "C": ["B", "D"],
    "D": ["B", "C", "E"],
    "E": ["D"]
  }
}
```

> **Nota del enunciado:** estos archivos solo **inicializan** el nodo (ID propio, canal y vecinos directos). **No** se usa para “conocer toda la topología” de antemano.

---

## 🧩 Archivos `.env` por nodo

Guárdalos en `./env/` (nombres de ejemplo: `A.env`, `B.env`, …).

Plantilla general:
```dotenv
# Identidad
SECTION=sec30
TOPO=topologia1
NODE=A

# Config paths
NAMES_PATH=./configs/names.json
TOPO_PATH=./configs/topo.json

# Redis (LOCAL)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PWD=

# Redis (REMOTO) - descomenta y comenta las anteriores para usarlo
# REDIS_HOST=homelab.fortiguate.com
# REDIS_PORT=16379
# REDIS_PWD=4YNydkHFPcayvlx7$zpKm

# Timers
HELLO_INTERVAL_SEC=3
INFO_INTERVAL_SEC=8
HELLO_TIMEOUT_SEC=15

# Logging
LOG_LEVEL=INFO

# Protocolo principal
PROTO=lsr
```

Cambia `NODE=` según corresponda en cada archivo (`A.env`, `B.env`, …).

---

## ▶️ Ejecución

Abre **5 terminales** (una por nodo) y levanta cada uno:

**Windows (PowerShell):**
```powershell
.\.venv\Scripts\Activate.ps1
python -m src.main --env env\A.env --show-table --wait 10
python -m src.main --env env\B.env --show-table --wait 10
python -m src.main --env env\C.env --show-table --wait 10
python -m src.main --env env\D.env --show-table --wait 10
python -m src.main --env env\E.env --show-table --wait 10
```

**macOS / Linux:**
```bash
source .venv/bin/activate
python -m src.main --env env/A.env --show-table --wait 10
# … y así para B, C, D, E
```

> La opción `--show-table` imprime la tabla de ruteo periódicamente; `--wait N` deja el nodo vivo N segundos tras el último comando CLI.

### Enviar un mensaje

Desde una **terminal extra**:
```bash
python -m src.main --env env/A.env --send E --body "hola desde A"
```
Verás en **E**: `DELIVERED A → E :: hola desde A`, y en B/C/D el tránsito `via <nexthop>` o `MSG-FLOOD` si aún no hay ruta.

---

## 🧪 Guía de pruebas (checklist)

1. **Arranque y vecinos**
   - Levanta A, luego B, C, D, E (en ese orden).  
   - Observa en B/C/D/E que llegan **HELLO** desde sus vecinos.
2. **LSR & Dijkstra**
   - Verifica que las **tablas de ruteo** se pueblan con costos 1 y rutas mínimas.
   - Desde A envía a E (`--send E`) y confirma en D `via E` y en E **DELIVERED**.
3. **Flooding controlado**
   - Envía un mensaje **antes** de que se consolide la tabla (apenas arranca) y observa `MSG-FLOOD` con de-dupe y TTL.
4. **Falla de enlace / nodo**
   - Cierra **D**; espera `Vecino sin HELLO: D` y **reconvergencia**.  
   - Intenta A→E: debe **fallar o flood** hasta que D regrese.
5. **Rejoin**
   - Relanza D y confirma que B/C/E actualizan rutas y vuelva el tránsito óptimo.
6. **TTL / anti-ciclos**
   - Observa que mensajes con TTL agotado **no** se reenvían; headers evitan loops.
7. **Estabilidad**
   - Deja correr 1–2 minutos; verifica que la LSDB expira orígenes inactivos, que los timers reanuncian INFO, y que la tabla se mantiene coherente.

> Consejo: ajusta `HELLO_INTERVAL_SEC`, `INFO_INTERVAL_SEC`, `HELLO_TIMEOUT_SEC` para pruebas más rápidas o estables.

---

## 📡 XMPP (segunda fase del laboratorio)

La base actual usa **Redis Pub/Sub** como transporte. Para cumplir la fase 2 (XMPP), se sugiere:

1. Implementar `XMPPTransport` con una interfaz equivalente a `RedisTransport`:  
   - `connect()`, `close()`, `publish_json(channel, payload)`, `broadcast(channels, payloads)`, `read_loop()` que `yield` mensajes JSON.  
2. Mapear `names.json` a **JIDs/recurso** en vez de canales Redis.  
3. Mantener **el mismo esquema de paquetes** (`proto/type/from/to/ttl/headers/payload`) para interoperabilidad.

> Con esta separación, **ForwardingService** y **RoutingLSRService** no cambian; solo el transporte.

---

## 🧰 Troubleshooting

- **No llegan HELLO/INFO** → revisa `REDIS_HOST`, `REDIS_PORT`, `REDIS_PWD`, firewall y que **todos** usan el mismo `names.json` y `topo.json`.
- **“Event loop is closed” al salir** (Windows): es benigno al cortar con Ctrl+C. Puedes mitigarlo llamando a `await node.stop()` (lo hace `main.py`) y/o usando Python 3.11+.
- **Unicode / colores raros**: usa la consola de Windows Terminal, fuente compatible y `chcp 65001`.
- **Nada se entrega en destino**: mira `MSG-FLOOD` → si no hay ruta establecida y TTL se agota, espera a que LSR converja y reintenta.
- **TLS/Autenticación XMPP**: (en la fase XMPP) valida credenciales, dominios y recurso; mantén JIDs únicos por nodo.

---

## 👥 Integrantes


| Nombre & Carné | Algoritmo |
|---|---|
| Gabriel Alberto Paz González  | Dijkstra |
| Andre Jo  | Flooding |
| Chrisitian | LSR


---

## 📚 Referencias

- Kurose & Ross, *Computer Networking: A Top-Down Approach* – Cap. enrutamiento (LSR, DVR).  
- Tanenbaum & Wetherall, *Computer Networks* – Algoritmos de ruteo.  
- RFC 2328 – **OSPF** (ejemplo de LSR industrial basado en Dijkstra).  
- Material de curso y enunciado del **Laboratorio 3 – Algoritmos de Enrutamiento**.

---

## 📝 Licencia

Uso académico.

---

