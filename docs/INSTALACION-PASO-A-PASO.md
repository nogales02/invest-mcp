# invest-mcp — Instalación paso a paso (para dummies)

Esta guía te lleva **desde cero** (un Windows sin nada) hasta tener el servidor
`invest-mcp` funcionando dentro de Claude, capaz de explorar, validar, ejecutar
e interpretar modelos de **InVEST** hablándole en lenguaje natural.

No hace falta saber programar. Vas a copiar y pegar comandos.

> **¿macOS / Linux?** Los conceptos son idénticos; usa `scripts/bootstrap.sh` en
> vez de `bootstrap.ps1` y rutas con `/`. El resto de la guía asume **Windows 10/11**.

---

## 0. Qué vas a montar (el mapa)

```
        Claude (Desktop o Code)
                │  habla "MCP" (un protocolo estándar)
                ▼
        invest-mcp   ← el servidor de este repo (Python ligero, vive en .venv)
          │   │   │
          │   │   └── env conda "invest-cal"  → calibración (natcap.invest + spotpy)
          │   └────── env conda "invest-geo"  → chequeos geoespaciales + resúmenes (GDAL)
          └────────── invest.exe               → el motor de InVEST (lo trae el Workbench)
```

Son **4 piezas**:

| Pieza | Qué es | La instalas en el paso |
|---|---|---|
| **Python 3.10+** | El lenguaje en el que corre el servidor | 1.1 |
| **InVEST Workbench** | La app de escritorio de InVEST. Trae `invest.exe` **y** un `micromamba.exe` que usamos para lo demás | 1.2 |
| **Este repo** (`invest-mcp`) | El servidor | 2 |
| **2 envs conda** (`invest-geo`, `invest-cal`) | Cajas aisladas con GDAL / natcap.invest. Se crean solas | 3 |

Nada de esto "se activa dentro de InVEST". `invest-mcp` es un programa aparte que
**llama** a `invest.exe` por debajo.

---

## 1. Instalar los requisitos

### 1.1 Python 3.10 o superior

1. Ve a <https://www.python.org/downloads/windows/> y descarga el instalador de
   la última versión 3.x (3.11, 3.12 o 3.13 valen; **3.14 aún no** para los envs).
2. Ejecuta el instalador y **marca la casilla `Add python.exe to PATH`** (abajo
   del todo). Si no la marcas, nada funcionará desde la terminal.
3. `Install Now`.
4. **Cierra y vuelve a abrir** cualquier terminal que tuvieras abierta.
5. Comprueba: abre **PowerShell** (menú Inicio → escribe `powershell`) y ejecuta:

   ```powershell
   python --version
   ```

   Debe responder `Python 3.11.x` (o 3.12 / 3.13). Si dice *"no se reconoce…"*,
   repite el instalador y asegúrate de la casilla PATH, o reinicia el equipo.

### 1.2 InVEST Workbench

1. Ve a <https://naturalcapitalproject.stanford.edu/software/invest> →
   **Download**. Elige el instalador de Windows (p. ej. *InVEST 3.20.1 Workbench*).
2. Instálalo con las opciones por defecto. Queda en:

   ```
   C:\Program Files\InVEST 3.20.1 Workbench\
   ```

   Dentro está lo que nos importa (no tienes que tocarlo, `invest-mcp` lo
   encuentra solo):

   ```
   ...\resources\invest\invest.exe      ← el motor de InVEST
   ...\resources\micromamba.exe         ← lo usamos para crear los envs conda
   ```

3. Abre el Workbench una vez para confirmar que arranca. Luego ciérralo.

> **¿Otra versión?** Da igual el número (3.14, 3.16, 3.20…). Si instalas varias,
> `invest-mcp` coge la más alta automáticamente.

> **¿No quieres el Workbench?** Alternativa solo para gente con conda:
> `conda create -n invest -c conda-forge natcap.invest` y añade su carpeta al
> PATH. Pero entonces no tienes el micromamba bundled y necesitarás tu propio
> conda/mamba para el paso 3. Con el Workbench es más simple.

### 1.3 Git (recomendado) — o descarga el ZIP

**Con Git** (te deja actualizar con un comando más adelante):

1. Descarga <https://git-scm.com/download/win>, instala con opciones por defecto.
2. `git --version` en PowerShell debe responder algo.

**Sin Git**: en la página del repo, botón verde **Code → Download ZIP**, y
descomprímelo donde quieras. Te saltas los `git clone` / `git pull` de esta guía.

### 1.4 Tu cliente: Claude Desktop o Claude Code

- **Claude Desktop**: <https://claude.ai/download> (app de Windows). La más fácil
  para empezar.
- **Claude Code**: la CLI (`npm install -g @anthropic-ai/claude-code` o el
  instalador). Para gente cómoda en terminal.

Cualquier otro cliente MCP (Cline, Continue, Cursor, LibreChat, mcphost para
Ollama…) también sirve — ver `INSTALL.md`.

---

## 2. Descargar el servidor

Elige una carpeta **sin espacios raros ni OneDrive** si puedes (p. ej.
`C:\Users\TU_USUARIO\invest-mcp` o `Y:\...\MCP_InVEST`).

**Con Git:**

```powershell
cd C:\Users\TU_USUARIO
git clone https://github.com/nogales02/invest-mcp
cd invest-mcp
```

**Con ZIP:** descomprime y entra en la carpeta. Ábrela con PowerShell:
en el Explorador de archivos, dentro de la carpeta, `Shift + clic derecho` en un
sitio vacío → *"Abrir ventana de PowerShell aquí"* (o *"Abrir en Terminal"*).

> **Importante:** para tener los chequeos geoespaciales y la calibración necesitas
> este `git clone` / ZIP **completo**. Un `pip install git+https://…` suelto no
> trae los ficheros `environment-*.yml` que hacen falta en el paso 3.

---

## 3. La vía rápida: el script `bootstrap`

Desde la carpeta del repo, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

Eso hace, en orden, **todo el paso 4 y 5 de golpe**:

1. busca tu Python ≥ 3.10,
2. crea el entorno `.venv` e instala `invest-mcp` dentro,
3. **crea los envs conda `invest-geo` y `invest-cal`** (usa el micromamba del
   Workbench). ⏳ **Esta parte tarda 10–25 min** — son descargas de GDAL y
   `natcap.invest`. Es normal que parezca parado; déjalo.
4. corre `invest-mcp doctor` para verificar,
5. imprime los comandos/JSON para registrar el server en tu cliente.

**Opciones útiles:**

| Comando | Para qué |
|---|---|
| `... bootstrap.ps1 -SkipEnvs` | Solo el servidor. Podrás explorar/validar/**ejecutar** modelos, pero no `preflight_geo` ni `run_calibration`. Rápido (~1 min). |
| `... bootstrap.ps1 -GeoOnly` | Servidor + solo `invest-geo` (chequeos y resúmenes, sin calibración). |
| `... bootstrap.ps1 -CalOnly` | Servidor + solo `invest-cal` (calibración). |
| `... bootstrap.ps1 -Conda "C:\ruta\micromamba.exe"` | Si no encuentra conda/mamba/micromamba solo. |
| `... bootstrap.ps1 -Http` | Añade `uvicorn` para servir por HTTP (`serve --transport streamable-http`). |

Si el script termina y `doctor` sale en verde, **salta al paso 5**. Si algo
falla, sigue con el paso 4 a mano para ver dónde.

> **Error `... no se puede cargar porque la ejecución de scripts está
> deshabilitada`** → usa exactamente la línea de arriba, con
> `-ExecutionPolicy Bypass`. No cambia nada permanente en tu sistema.

---

## 4. La vía manual (si el script falla, o para entender qué pasa)

Todo desde la carpeta del repo, en PowerShell.

### 4.1 Crear el `.venv` (entorno Python aislado del servidor)

```powershell
python -m venv .venv
```

Aparece una carpeta `.venv\`. **No la borres.** Contiene su propio `python.exe`
en `.venv\Scripts\python.exe`; usaremos esa ruta para todo.

### 4.2 Instalar `invest-mcp` en el `.venv`

```powershell
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .
```

El `-e` (editable) hace que, si luego actualizas el repo con `git pull`, los
cambios se recojan sin reinstalar. Ahora ya tienes el comando `invest-mcp`:

```powershell
.\.venv\Scripts\python -m invest_mcp --version
```

### 4.3 Crear los envs conda (`invest-geo` + `invest-cal`)

Estos llevan GDAL y `natcap.invest`, que **no se pueden instalar con `pip` en
Windows** de forma limpia, así que van en envs conda aparte. Un solo comando los
construye desde los `environment-*.yml` del repo:

```powershell
.\.venv\Scripts\python -m invest_mcp setup            # los dos
.\.venv\Scripts\python -m invest_mcp setup --geo      # solo invest-geo
.\.venv\Scripts\python -m invest_mcp setup --cal      # solo invest-cal
```

**Qué usa para construirlos**, en este orden: la variable `CONDA_EXE` / `MAMBA_EXE`
si existe → un `micromamba` en el PATH → **el `micromamba.exe` que trae el InVEST
Workbench** (por eso con el Workbench no necesitas instalar conda) → un
`conda.exe` / `mamba.exe` real bajo una instalación detectada. Nunca usa los
wrappers `condabin\*.bat` (crashean al crear envs). Fuerza uno con
`setup --conda <ruta>`.

**Dónde quedan los envs:**

| Herramienta | Ubicación de los envs |
|---|---|
| conda / mamba | `<raíz-conda>\envs\invest-geo` |
| **micromamba** (el del Workbench) | `%APPDATA%\mamba\envs\invest-geo` |

`invest-mcp doctor` y el servidor los encuentran en cualquiera de esos sitios.
Si usaste una ubicación rara, `setup` imprime la ruta exacta del `python.exe` que
creó — cópiala a `INVEST_MCP_GEO_PYTHON` / `INVEST_MCP_CAL_PYTHON` (ver paso 8).

`setup` es **idempotente**: si un env ya existe, la creación es un no-op y solo
refresca el `pip install -e .` dentro. Re-ejecútalo sin miedo tras un `git pull`.

### 4.4 Verificar con `doctor`

```powershell
.\.venv\Scripts\python -m invest_mcp doctor
```

Salida ideal:

```
invest-mcp 0.1.0

[ok]   invest      C:\Program Files\InVEST 3.20.1 Workbench\...\invest.exe  (v3.20.1)
[ok]   invest-geo  C:\Users\tu\AppData\Roaming\mamba\envs\invest-geo\python.exe
[ok]   invest-cal  C:\Users\tu\.conda\envs\invest-cal\python.exe
[ok]   data root   C:\Users\tu\invest-mcp-data

allowed input roots:
  - C:\Users\tu\invest-mcp-data
  - C:\Users\tu\invest-mcp

all good
```

- `[ok] invest` es **obligatorio**. Si sale `[FAIL]`, ve al paso 10.
- `[warn] invest-geo` / `invest-cal` solo significa *"esa función está apagada
  hasta que corras `setup`"*. El servidor arranca igual.

---

## 5. Registrar el servidor en tu cliente

Primero, ten a mano el bloque que imprime:

```powershell
.\.venv\Scripts\python -m invest_mcp mcp-config                    # JSON genérico
.\.venv\Scripts\python -m invest_mcp mcp-config --client claude-code   # comando de Claude Code
```

### 5.a Claude Desktop

1. Cierra Claude Desktop del todo (icono en la bandeja → *Quit*).
2. Abre el fichero de config. En el Explorador, pega en la barra de direcciones:

   ```
   %APPDATA%\Claude
   ```

   Abre `claude_desktop_config.json` con el Bloc de notas. Si no existe, créalo.
3. Pega el contenido que imprimió `mcp-config` (el JSON con `"mcpServers"`).
   Si el fichero ya tenía otros servidores, **fusiona** — deja una sola clave
   `"mcpServers"` con todas las entradas dentro. Ejemplo final:

   ```json
   {
     "mcpServers": {
       "invest": {
         "command": "C:\\Users\\tu\\invest-mcp\\.venv\\Scripts\\python.exe",
         "args": ["-m", "invest_mcp"],
         "env": {
           "INVEST_MCP_INVEST_EXE": "C:\\Program Files\\InVEST 3.20.1 Workbench\\resources\\invest\\invest.exe"
         }
       }
     }
   }
   ```

   Ojo con las **barras dobles** `\\` en las rutas (es JSON).
4. Guarda. Abre Claude Desktop.
5. En una conversación nueva, el icono de herramientas (🔌 / martillo) debe listar
   el servidor **invest**. Escribe: *"lista los modelos de InVEST"*.

### 5.b Claude Code

Ejecuta el comando que imprimió `mcp-config --client claude-code`. Tiene esta
forma:

```powershell
claude mcp add invest --scope user --env "INVEST_MCP_INVEST_EXE=C:\Program Files\InVEST 3.20.1 Workbench\resources\invest\invest.exe" -- "C:\Users\tu\invest-mcp\.venv\Scripts\python.exe" -m invest_mcp
```

- `--scope user` lo deja disponible en todos tus proyectos. Usa `--scope local`
  para solo la carpeta actual.
- Comprueba con `claude mcp list` → debe decir `invest: connected`.
- Reinicia Claude Code (`/exit` y volver a entrar, o `claude --continue`).

---

## 6. Primera prueba real (modelo Carbon con datos de muestra)

1. **Baja datos de muestra de InVEST.** Desde el Workbench:
   *File → Download Sample Data → Carbon*. O directo:
   <https://storage.googleapis.com/releases.naturalcapitalproject.org/invest/3.20.1/data/Carbon.zip>
   Descomprime, p. ej. en `C:\Users\tu\invest-sample-data\Carbon\`.

2. En Claude, pídele por ejemplo:

   > Confirma el entorno de InVEST. Luego, permite la carpeta
   > `C:\Users\tu\invest-sample-data\Carbon` como origen de inputs, describe el
   > modelo `carbon`, valida unos args con el LULC y la tabla de carbono de esa
   > carpeta, y si está OK, ejecútalo y resume los resultados.

   El asistente irá llamando a las tools: `invest_env` → `allow_input_dir` →
   `describe_invest_model` → `validate_invest_args` → `run_invest_model` →
   `get_invest_job` → `summarize_results`.

3. Las salidas quedan en
   `C:\Users\tu\invest-mcp-data\jobs\<job_id>\workspace\`.

---

## 7. Rutas y carpetas: qué vive dónde

| Cosa | Ruta por defecto |
|---|---|
| Motor InVEST | `C:\Program Files\InVEST <ver> Workbench\resources\invest\invest.exe` |
| Servidor (`.venv`) | `<repo>\.venv\Scripts\python.exe` |
| Env geoespacial | `%APPDATA%\mamba\envs\invest-geo` **o** `%USERPROFILE%\.conda\envs\invest-geo` |
| Env calibración | `…\envs\invest-cal` (mismo esquema) |
| Jobs, logs, provenance | `%USERPROFILE%\invest-mcp-data\` |
| Carpetas de inputs permitidas | el *data root* + el directorio de trabajo del server. Amplías con `allow_input_dir` (por sesión) o `INVEST_MCP_ALLOWED_INPUT_DIRS` |

**Seguridad:** el servidor solo lee ficheros dentro de esa lista blanca. **Nunca**
la apuntes a la raíz de un disco (`C:\`, `Y:\`).

---

## 8. Configuración opcional (`INVEST_MCP_*`)

Todo es opcional; se autodetecta. Para forzar algo, crea un fichero `.env` (copia
de `.env.example`) donde lances el servidor, o define variables de entorno reales.
En Claude Desktop van en el bloque `"env": { … }` del JSON.

| Variable | Default | Para qué |
|---|---|---|
| `INVEST_MCP_INVEST_EXE` | autodetección del Workbench | Ruta a `invest.exe` si tienes una instalación no estándar |
| `INVEST_MCP_GEO_PYTHON` | autodetección `invest-geo` | `python.exe` del env geo si quedó en sitio raro |
| `INVEST_MCP_CAL_PYTHON` | autodetección `invest-cal` | ídem para calibración |
| `INVEST_MCP_DATA_ROOT` | `%USERPROFILE%\invest-mcp-data` | Dónde escribir jobs/logs |
| `INVEST_MCP_ALLOWED_INPUT_DIRS` | — | Carpetas extra de inputs (`;` entre rutas) |
| `INVEST_MCP_MAX_CONCURRENT_JOBS` | `2` | Runs en paralelo |
| `INVEST_MCP_INVEST_TIMEOUT_SECONDS` | `21600` | Timeout por run (6 h) |
| `INVEST_MCP_LOCALE` | — | Idioma de mensajes InVEST (`es` / `en` / `zh`) |
| `INVEST_MCP_TRANSPORT` / `_HOST` / `_PORT` | `stdio` / `127.0.0.1` / `8000` | Solo para servir por HTTP (necesita el extra `[http]`) |

---

## 9. Actualizar a una versión nueva

```powershell
cd <repo>
git pull
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1        # re-hace .venv + envs (idempotente)
# o a mano:
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m invest_mcp setup
.\.venv\Scripts\python -m invest_mcp doctor
```

Tras actualizar, **reinicia tu cliente** (Claude Desktop / Claude Code) para que
recargue el servidor.

---

## 10. Problemas típicos

| Síntoma | Causa / arreglo |
|---|---|
| `python` *"no se reconoce…"* | No marcaste *Add to PATH* al instalar Python. Reinstala con esa casilla, abre una terminal nueva. |
| `bootstrap.ps1` *"la ejecución de scripts está deshabilitada"* | Lánzalo con `powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1`. No cambia nada permanente. |
| `doctor` → `invest [FAIL]` | No hay Workbench. Instálalo, o define `INVEST_MCP_INVEST_EXE` con la ruta a un `invest.exe` / `invest`. |
| `setup` → *"No conda/mamba/micromamba found"* | Instala el InVEST Workbench (trae micromamba) o [Miniforge](https://github.com/conda-forge/miniforge), o pasa `--conda <ruta a micromamba.exe>`. |
| `setup` creó el env pero `doctor` dice *not found* | Quedó en una ruta que la detección no mira. `setup` imprime la ruta que usó → ponla en `INVEST_MCP_GEO_PYTHON` / `INVEST_MCP_CAL_PYTHON`. |
| `setup` falla con un crash de `condabin\*.bat` / *"Could not open lockfile"* | Estás pasando un wrapper `.bat`. Usa `--conda` apuntando a un `micromamba.exe` / `conda.exe` **real**. |
| Los envs tardan muchísimo / parece colgado | Normal: GDAL + natcap.invest son cientos de MB. Déjalo 20–30 min. Si de verdad se cuelga, `Ctrl+C` y re-ejecuta `setup` (retoma). |
| El antivirus bloquea `micromamba.exe` | Añade una excepción para `C:\Program Files\InVEST … \resources\micromamba.exe` y para `%APPDATA%\mamba`. |
| Claude Desktop no muestra el servidor | ¿Fusionaste bien el JSON (una sola clave `mcpServers`)? ¿Barras dobles `\\` en las rutas? ¿Cerraste y reabriste la app del todo? Mira los logs en `%APPDATA%\Claude\logs\`. |
| Claude Code: `claude mcp list` → `failed` | Ejecuta a mano `<.venv>\Scripts\python.exe -m invest_mcp` — si peta, el error sale ahí. Suele ser una ruta mal escrita en el `mcp add`. |
| Ruta de inputs *"outside every allowed folder"* | Usa la tool `allow_input_dir("C:\\ruta")` en la conversación, o añade la carpeta a `INVEST_MCP_ALLOWED_INPUT_DIRS` y reinicia. |
| El `.jpg` de dotty plots de calibración no aparece | Conocido: algunos builds de matplotlib en conda crashean al guardar PNG en esta clase de máquina. El `FIGURES/dotty_data_<MODELO>.json` (datos crudos) siempre se escribe; el run no se ve afectado. |
| `invest --help` peta con `UnicodeEncodeError` si lo lanzas tú | Es un bug del `invest.exe` bundled con consola cp1252. `invest-mcp` ya lo sortea (fuerza UTF-8); no lo llames tú directamente. |

---

## Resumen de un vistazo

```powershell
# 1-2. instala Python 3.10+ (con PATH) y el InVEST Workbench; clona el repo
git clone https://github.com/nogales02/invest-mcp
cd invest-mcp

# 3. una línea lo monta todo (~10-25 min por los envs conda)
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

# 4. registra en tu cliente con lo que imprime:
.\.venv\Scripts\python -m invest_mcp mcp-config --client claude-code

# 5. reinicia el cliente y prueba: "lista los modelos de InVEST"
```
