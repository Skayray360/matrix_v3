<!-- Creado por Aldo Garcia. -->
# Instalar o actualizar Matrix RH en la misma carpeta

La raíz de trabajo es **`C:\wamp64\www\matrix-rh-1.3.0`**. Esta revisión conserva
la versión interna 1.3.1 que ya traía el código del ZIP recibido. No exige crear
otra carpeta, trasladar el proyecto ni cambiar el nombre de la base de datos.

| Situación | Procedimiento |
| --- | --- |
| Ya hay usuarios, PDF o historial | Actualización sobre la carpeta actual, sección 1 |
| Primera instalación en ese equipo | Preparación e instalación, sección 2 |
| Recuperar una caída | [Operación y restauración](RUNBOOK.md) |

## 1. Actualizar conservando la instalación actual

1. Desde la carpeta existente, ejecutar **DETENER_MATRIX_RH.bat**. Si falla o siguen
   procesos activos, resolverlo antes de sustituir código; no iniciar otra API.
2. Respaldar **.env**, la base SQL, **data/**, **var/** y YAML personalizados de
   **config/**. Incluir rutas externas configuradas. Conservar el respaldo fuera
   del proyecto y comprobar que se puede restaurar. No compartirlo: contiene
   datos y secretos.
3. Sustituir el **contenido del proyecto** por el del ZIP entregado en la misma
   raíz. Al terminar, README.md, backend/ y los cuatro BAT deben estar directamente
   dentro de matrix-rh-1.3.0, nunca en una carpeta doble como
   **matrix-rh-1.3.0\matrix-rh-1.3.0**.
4. Conservar **.env, .venv, var/, SQL y documentos existentes**. El ZIP no incluye
   .env ni estado de var/ y no es un respaldo del historial. No reemplazar
   documentos más recientes por copias antiguas del paquete. Si TI personalizó
   YAML de permisos o fuentes, comparar y conservar esos ajustes: una sustitución
   genérica no debe restablecer autorizaciones.
5. Iniciar MySQL de WAMP y Ollama. Desde la misma carpeta ejecutar:

```powershell
Set-Location 'C:\wamp64\www\matrix-rh-1.3.0'
.\INSTALAR_MATRIX_RH.bat
```

El instalador admite repetir la operación: sincroniza dependencias fijadas,
actualiza configuración operativa con respaldo de .env, comprueba modelos, aplica
migraciones pendientes e ingiere el corpus salvo **-SkipIngest**. Conserva la
conexión SQL existente; no crea otra base por el nombre de esta revisión.

La limpieza de archivos heredados se aplica dentro de esa instalación: los tres
BAT retirados se guardan fuera de la raíz, en **var/backups/layout-*/**, con
extensión **.retired**. Los 64 Markdown obsoletos se retiran sólo si coinciden
con los SHA conocidos; se conservan los personalizados. Los assets antiguos sólo
se retiran si su hash es conocido y no están referenciados por el build vigente.
Esto no elimina PDF, índices, conversaciones ni configuraciones personalizadas.

**No ejecutar el antiguo actualizador entre carpetas.** Los únicos lanzadores
vigentes son instalar, diagnosticar, iniciar y detener. La corrección RAG está
integrada; no aplicar después un parche de una entrega anterior.

Una actualización puede modificar configuración y esquema SQL, por eso el respaldo
precede a la instalación. Un rollback requiere restaurar un conjunto compatible de
código, configuración, SQL y archivos; volver sólo al código no revierte el esquema.

## 2. Preparar una primera instalación

- Windows 11 x64 y **Python 3.12 x64**; comprobar **py -3.12 --version**.
- WAMP con MySQL/MariaDB activo y sus credenciales/puerto reales.
- Ollama con **gemma4:latest** y **embeddinggemma:latest** en **ollama list**.
- Acceso al índice de paquetes aprobado o cachés preparadas para Windows x64.
- Memoria, almacenamiento y GPU adecuados al modelo; medir capacidad en destino.

WAMP aporta la base de datos. FastAPI sirve el chat; no hace falta configurar PHP
ni Apache para abrirlo. El frontend compilado está incluido: Node.js sólo se
requiere para desarrollo o **-RebuildFrontend**. Python, WAMP, Ollama y los pesos
no vienen dentro del ZIP.

Extraer en **C:\wamp64\www\matrix-rh-1.3.0** y ejecutar **INSTALAR_MATRIX_RH.bat**.
Si no existen, el instalador crea .env, secretos y .venv. No copiar un entorno
virtual de otra ruta. Los BAT aplican ExecutionPolicy Bypass exclusivamente al
proceso de PowerShell utilizado, sin cambiar la política global.

La plantilla de una instalación nueva usa **matrix_rh_131** en MySQL local 3306.
Ese nombre no cambia una base ya configurada. root sin contraseña es sólo el
ejemplo de desarrollo WAMP: ajustar DATABASE_URL privadamente a las credenciales
reales; no debilitar el servidor para adaptarlo al ejemplo. Una base ajena no se
borra ni se adopta automáticamente.

El instalador no descarga ni reemplaza modelos. Verifica digests de Ollama,
completa pins vacíos y rechaza discrepancias en los existentes. No borrar pins
para ocultar un cambio de pesos. Opciones: [herramientas Windows](../windows/README.md).

## 3. Abrir, comprobar y entrar

```powershell
.\INICIAR_MATRIX_RH.bat
curl.exe --silent --show-error --noproxy "*" --max-time 20 "http://127.0.0.1:8000/ready"
```

Abrir **http://127.0.0.1:8000**. UI y API comparten origen. No usar varios workers.
La reserva de IA de esta revisión identifica al proceso propietario para recuperar
una caída local comprobada sin esperar su vencimiento. La carga de dependencias
y modelos sí necesita tiempo; ver límites y reservas antiguas en
[Diagnóstico](TROUBLESHOOTING.md).

| Usuario de prueba inicial | Alcance |
| --- | --- |
| Matrix | Administración de prueba y categorías de negocio concedidas |
| MatrixR1 | Lectura de prestaciones, sin administración |

En una instalación existente se conservan las cuentas. Para una instalación nueva,
consultar la clave generada sólo en la terminal local:

```powershell
Select-String -LiteralPath '.\.env' -Pattern '^MATRIX_SEED_PASSWORD='
```

Usar el valor después de =; no compartir la clave ni .env. Reinstalar no restablece
contraseñas ya existentes. local_test es un perfil de prueba en loopback; no
sustituye autenticación empresarial.

En VS Code, después de instalar y sin otra API activa, se puede ver el log en
primer plano desde la raíz:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& .\.venv\Scripts\python.exe -u -m scripts.bootstrap serve
```

Ctrl+C solicita cierre; DETENER_MATRIX_RH.bat identifica también los procesos de
esta raíz cuando el arranque manual utiliza su .venv y el comando admitido.

## 4. Documentos y aceptación funcional

Los documentos del ZIP recibido se conservan en la revisión. Copiar otros PDF
aprobados una sola vez en la categoría/alias correspondiente, sin duplicarlos.
Copiar un archivo no lo indexa ni concede acceso. PDF escaneado necesita OCR
previo; esta aplicación no lo ejecuta. Ver [RAG y corpus](RAG_DESIGN.md).

Para volver a ingerir, detener Matrix si se utiliza Qdrant embebido:

```powershell
.\DETENER_MATRIX_RH.bat
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& .\.venv\Scripts\python.exe -m scripts.bootstrap knowledge-status
& .\.venv\Scripts\python.exe -m scripts.bootstrap ingest
.\INICIAR_MATRIX_RH.bat
```

Revisar errores por archivo y repetir las preguntas sobre préstamos y pensiones
en una conversación nueva, comprobando respuestas y fuentes en los PDF originales.
**ready=true** no acredita por sí solo calidad documental ni generación correcta.
Completar las [pruebas de aceptación](TESTING.md#aceptación-manual-en-el-equipo-destino).

## 5. Diagnosticar y detener

```powershell
.\DIAGNOSTICO_MATRIX_RH.bat
# Opcional, sin backend activo: inferencia y embeddings sintéticos.
.\DIAGNOSTICO_MATRIX_RH.bat -ProbarModelos
.\DETENER_MATRIX_RH.bat
```

El diagnóstico ordinario consulta estado y metadata. La opción de modelos es
activa, consume recursos y no prueba los PDF empresariales. La parada termina
procesos identificados de esta instancia; no termina WAMP, Ollama ni procesos
ajenos compartidos. Resultados actuales y límites:
[informe de validación](../reports/VALIDACION_1.3.1.md).
