<!-- Creado por Aldo Garcia. -->

# scripts/

Scripts de shell para entornos no-Windows.

`matrixrh.sh` es el equivalente de los `.bat` para entornos no-Windows y delega
en los mismos módulos Python (no duplica lógica). Subcomandos: `install`,
`start`, `stop`, `diagnose` y `validate`. Al igual que el instalador de Windows,
`install` usa `uv sync --frozen`,
`corepack npm ci --ignore-scripts --no-audit --no-fund` y verifica la cadena
de suministro antes de compilar el frontend.

`validate` no tiene lanzador `.bat` en la raíz (en Windows se ejecuta con
`powershell -File windows\Validate-MatrixRH.ps1`); aquí se conserva como
subcomando por comodidad en Linux/macOS. Antes de detener el servicio o
modificar datos, exige `scripts.test_environment`: `APP_ENV=test`, base
`matrix_rh_test` o `matrix_rh_test_<sufijo>`, consentimiento
`MATRIX_TEST_ALLOW_DESTRUCTIVE=true` y uploads/índice dentro de `var/tests`.
Después delega en el gate común `scripts.run_quality_gate --fast`. Este alcance
no ejecuta generación RAG ni E2E completo; un paso obligatorio omitido devuelve
`INCOMPLETE` (código 2), y cualquier fallo devuelve un código no cero.

```bash
./scripts/matrixrh.sh install
```
```bash
./scripts/matrixrh.sh start
```

En Windows use los `.bat` de la raíz.
