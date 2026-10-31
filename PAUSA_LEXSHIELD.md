# LexShield en pausa (2026-10-31)

Documento para retomar el proyecto sin volver a investigar. **No contiene secretos**: solo nombres de variables y
dónde se configuran. Este repositorio es **público**.

Al pausar no hay nada integrado en `main`, nada desplegado y ninguna llamada a OpenAI autorizada.

---

## 1. Estado al pausar

### Auditor de contratos (frente de Claude)

| Tema | Estado |
|---|---|
| Candidata 2 | **Construida.** Añade un control local de rescate: las fichas del auditor con cita literal que una mesa posterior descarta sin un motivo comprobado se listan aparte, como "Hallazgos detectados pero no conservados por la revisión", con el estado "Pendiente de revisión humana; no validado por el sistema". Nunca como hallazgo confirmado |
| Candidata 2b | **Construida** (ya cubre los pasos 2a y 2b de abajo). Los registros de descarte del modelo se validan localmente; solo un motivo comprobado o un control local determinista impiden un rescate. Añade la métrica técnica `review_reliability` |
| Rescate en c2 reconstruido | **Funciona** con las respuestas guardadas de las ejecuciones #12, #14 y #15: rescata la renovación automática del Art. 9 y nada más. Ningún falso positivo en las 13 ejecuciones guardadas |
| c2 real con la candidata 2 (ejecución #15) | Renovación **rescatada** y bien etiquetada; los 4 hallazgos esperados, publicados. **El socio es poco fiable**: no registró su descarte real y registró uno de una ficha inexistente (Art. 5), que el supervisor copió |
| c13 (caso reservado) | **No ejecutada** con la candidata 2/2b. Sigue sin medirse si el rescate llena el informe en un contrato nuevo |
| Veredicto | Candidata 2 **no apta todavía** (falta c13 y la variación). La 2b está pendiente de validar |
| Gasto de OpenAI (evaluación) | **1,1252 USD de 1,50** (60 llamadas; estimado por tokens × precio público, no contrastado con la factura) |
| Límite vigente | **1,50 USD acumulado. Sin llamadas autorizadas**: cada ejecución necesita autorización expresa |

### Pagos y acceso (frente de Cursor)

Paddle sandbox, en un worktree aparte. Las reglas comunes están en `docs/CONTRATO_COORDINACION.md` (identidad,
estados de acceso, esquemas y archivos de cada agente). Decisiones comerciales vigentes:
- precio de 150 USD al mes, con un 50 % de descuento solo el primer mes; sin prueba gratuita;
- `past_due` bloquea los análisis nuevos para todos los proveedores;
- nunca configurar 100 USD ni el 80 % de descuento.

Hoy `main` del frontend y del backend todavía anuncia 100 USD y FOUNDER80: hay que corregirlo antes de cualquier
lanzamiento.

---

## 2. Próximos pasos, en orden

| | Paso | Estado | Necesita |
|---|---|---|---|
| a | Endurecer el control local de descartes, sin API | **Hecho** (candidata 2b, `e171788`, local) | — |
| b | Probarlo con las respuestas guardadas de la ejecución 15 | **Hecho** (175 pruebas en verde en la candidata integrada `b2d764b`) | — |
| c | Ejecutar c13 | Pendiente | **Autorización expresa**. Reserva exacta: 0,3447 USD; caben dentro de 1,50 (quedan 0,3748) |
| d | Evaluar si el supervisor justifica su coste | Pendiente | En las muestras guardadas copia al socio, supone el 37 % del coste y añadió más errores que aciertos. Decisión de arquitectura: requiere una candidata nueva y validación real |
| e | Completar Paddle sandbox con Cursor | En curso (Cursor) | Contrato de coordinación |
| f | Validar una auditoría real antes de la beta | Pendiente | Autorización y presupuesto de beta |

---

## 3. Reglas vigentes

- **No confiar en los registros de descarte del modelo.** El socio los inventa u omite; solo cuentan los motivos
  comprobados localmente.
- **No cambiar prompts sin declarar una candidata nueva.** `test_prompt_freeze.py` falla si cambian (hash vigente:
  `53588445a940a15b`).
- **No gastar el saldo destinado a la beta.** Antes de llamar: reserva exacta con `tiktoken`, reintentos
  desactivados y `--expect-app`.
- **No hacer push, merge ni deploy sin autorización.** Empujar ramas del frontend crea despliegues de vista previa
  en Vercel; `main` del frontend se despliega en producción.
- **No declarar lista la beta sin validación real.**
- No tocar los cambios sin commit de otros (hay unos "beta guards" sin commit en el checkout original del backend).
- No leer ni mostrar `.env`, claves, tokens ni contraseñas. No hacer pagos reales ni activar los bypasses de
  autenticación contra producción.

---

## 4. Mapa de archivos

**En esta rama (`docs/agent-coordination`, empujada):**
- `PAUSA_LEXSHIELD.md`: este documento.
- `docs/CONTRATO_COORDINACION.md`: contrato con Cursor (identidad y sesiones, estados de acceso, esquemas de la API,
  archivos de cada agente, candidatas integradas).

**Solo en local, sin empujar** (rama `eval/auditor-real-calls`, worktree `lexshield-worktrees/backend-eval`):

| Archivo | Contenido |
|---|---|
| `eval/CANDIDATA_AUDITOR_2.md` | Diseño del rescate, pruebas, informe de c2 reconstruido, falsos positivos potenciales |
| `eval/CANDIDATA_AUDITOR_2B.md` | Endurecimiento de los descartes, métrica de fiabilidad y **plan de validación pendiente de c13** |
| `eval/RESULTADOS_candidata2.md` | Ejecución #15 (c2 real): resultados, criterios y veredicto |
| `eval/RESERVA_POR_TOKENS.md` | Fórmula de la reserva exacta, calibración y coste máximo por llamada |
| `eval/RUNS.md` | Registro de las 15 ejecuciones reales, con IDs, commits, prompts y coste |
| `eval/CONCILIACION_LEDGER.md` | Gasto por ejecución; reservas frente a consumo |
| `eval/RASTREO_ETAPAS.md` | Qué pierde, inventa o copia cada mesa |
| `eval/CANDIDATA_AUDITOR_1.md`, `eval/PLAN_PRUEBA_FINAL.md`, `eval/CIERRE_AUDITOR.md` | Historia anterior |
| `eval/runs/ledger.jsonl` | Registro de gasto, una fila por llamada (ignorado por git, nunca se edita) |

**Ramas locales del código** (sin empujar; etiqueta local `pausa-2026-10-31-candidata-2` en las dos integradas):

| Repositorio | Rama | Commit |
|---|---|---|
| backend | `integration/auditor-candidate-2` (candidata 2b integrada; 175 pruebas) | `b2d764b` |
| backend | `integration/auditor-candidate-1` | `971588d` |
| backend | `fix/auditor-local-controls` (etiquetas `auditor-candidata-1`, `-2`, `-2b`) | `e171788` |
| backend | `eval/auditor-real-calls` | `0f9d263` |
| frontend | `integration/report-candidate-2` | `66eecb7` |
| frontend | `integration/report-candidate-1` | `021d2bd` |
| frontend | `fix/report-evidence-and-states` | `8b21f0f` |

Otras ramas locales: `fix/openai-quota-honest-error`, `feat/simulated-analysis-for-tests` y
`feat/simulated-analysis-banner`. Pila de PRs de facturación abierta en GitHub: backend #1 y #3–#9, frontend #5–#9,
ninguno integrado.

---

## 5. Servicios y accesos (sin valores)

| Servicio | Uso | Dónde se configura | Estado |
|---|---|---|---|
| GitHub | `josuedavidperezrostran-boop/lexshield-backend` y `…/lexshield-frontend` | GitHub | **Ambos públicos** |
| Render | Backend (URL que usa el frontend: `https://lexshield-backend.onrender.com`) | Variables de entorno del servicio en Render | **Servicio no localizado** en el panel del usuario; el despliegue automático no está confirmado |
| Vercel | Frontend: `main` → producción; cada rama empujada → vista previa | Variables del proyecto en Vercel | Activo |
| Stripe | Solo modo test | Variables del backend; panel de Stripe | Destinos de webhook vacíos en Workbench; no se sabe si hay clientes reales |
| Paddle | Sandbox (cuentas sandbox y live; live no se da por aprobada) | Variables del backend | Sin recursos creados por Claude; lo lleva Cursor |
| OpenAI | Análisis (`gpt-4o`) | Variable del backend | Saldo cargado por el usuario; llamadas en pausa |
| Dominio | — | — | **Sin dominio propio** |
| Correo | Códigos de un solo uso para alta y recuperación | Variable `MAIL_BACKEND` | **Sin proveedor**; adaptador real sin escribir |

**Variables del backend** (`app/config.py`; se leen del entorno o del `.env` dos carpetas por encima de `app/`, en
local `LexShield/.env`):
- **en `main`:** `OPENAI_API_KEY`, `OPENAI_MODEL`, `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
  `STRIPE_WEBHOOK_SECRET`, `PARTNERS_DB_PATH`, `ACCESS_TOKEN_SECRET`, `FRONTEND_ORIGIN`, `BACKEND_ORIGIN`,
  `SUBSCRIPTION_AMOUNT_CENTS`, `MAX_UPLOAD_BYTES`, `MAX_CONTRACT_CHARS`, `DEV_BYPASS_BILLING`;
- **añadidas en la pila de facturación (`feat/email-verified-checkout`):** `ENVIRONMENT`, `SUBSCRIPTION_CACHE_SECONDS`,
  `SHARED_STORE_URL`, `MAIL_BACKEND`, `TRUST_FORWARDED_FOR`, `STRIPE_MODE`, `STRIPE_PRICE_ID_TEST`,
  `STRIPE_PRICE_ID_LIVE`, `STRIPE_PORTAL_CONFIGURATION_ID_TEST`, `STRIPE_PORTAL_CONFIGURATION_ID_LIVE`,
  `STRIPE_AUTHORIZED_PRODUCT_IDS`, `STRIPE_AUTHORIZED_PRICE_IDS`;
- **otras:** `PADDLE_SANDBOX_WEBHOOK_SECRET` (`app/paddle_webhooks.py`), `PLAYBOOK_CONFIGURED` (`app/playbook.py`),
  `ANALYSIS_BACKEND` (solo `feat/simulated-analysis-for-tests`) y `RENDER` (la pone Render; con
  `ENVIRONMENT=production` desactiva los atajos de desarrollo).

**Variables del frontend** (`next.config.ts`; en Vercel o en el `.env` de la carpeta superior en local):
`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SITE_URL` y `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` (o `STRIPE_PUBLISHABLE_KEY`).
En producción, o con una URL local, la API apunta siempre a la de Render.

**Evaluación:** el arnés lee `OPENAI_API_KEY` y `OPENAI_MODEL` de `LexShield/.env` sin imprimirlos. `tiktoken`
está instalado en el entorno virtual del backend solo para contar tokens.

---

## Cómo retomar

1. Leer `docs/CONTRATO_COORDINACION.md` y este documento.
2. Para revisar el código: el worktree `backend-integration-2` (rama `integration/auditor-candidate-2`). Con la clave
   de OpenAI vacía, `python -m unittest discover -p "test_*.py"` debe dar 175 pruebas en verde.
3. Antes de cualquier llamada real: autorización expresa, `python -m eval.estimate <caso> --output-cap 6000` desde el
   worktree integrado (con `PYTHONPATH` apuntando al worktree de evaluación) para la reserva exacta, y ejecutar con
   `--expect-app`.
