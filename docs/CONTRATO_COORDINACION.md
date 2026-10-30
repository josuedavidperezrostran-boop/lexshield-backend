# Contrato de coordinación entre agentes

Fecha: 2026-10-30. Agentes: **Claude** (frente auditor) y **Cursor** (frente Paddle sandbox).
Sirve para trabajar en paralelo sin pisarse. Si hay que cambiar algo de aquí, primero se cambia este documento
(con una línea en el registro del final) y después el código.

Base de referencia: la cima de la pila de PRs del backend, `feat/email-verified-checkout` (`fcf2104`), que contiene
`auth.py` v2, `sessions.py`, `store.py`, `subscriptions.py`, `signup.py` y `recovery.py`. Nada de la pila está
integrado en `main`. La rama base del trabajo de Paddle la decide el usuario; recomendación: partir de esa cima.

## 1. Identidad y sesiones (lo que existe; Cursor puede extenderlo)

- **Token de acceso** (`app/auth.py`): firmado con HMAC usando `ACCESS_TOKEN_SECRET`, formato `v2`. Campos:
  `issued_at`, `auth_at` (momento de la prueba de identidad que abrió la sesión), `session_id` y `customer_id`.
  Vigencia de 7 días, renovable durante 60 días con `/billing/refresh`, sesión máxima de 90 días desde `auth_at`.
  En producción, si el secreto falta o es el de por defecto, no se emiten tokens (503).
- **Sesiones** (`app/sessions.py` sobre `app/store.py`): revocables una a una (`/billing/logout`) o todas las de un
  cliente (`/billing/sessions/revoke-all`). El almacén es compartido (memoria en pruebas, SQLite en una instancia).
- **Prueba de identidad**: código de un solo uso enviado por correo (`signup.py`, `recovery.py`). Un correo, un
  `session_id` de Checkout o un identificador enviado por el navegador **no** prueban identidad ni dan acceso.
- **Hoy `customer_id` es un id de cliente de Stripe** (`cus_…`). Para Paddle hay que distinguir proveedor. Reglas del
  contrato:
  1. El sujeto del token identifica al proveedor sin ambigüedad (por ejemplo `stripe:cus_…` / `paddle:ctm_…`, o un id
     de cuenta interno asociado a ambos). Lo elige Cursor y lo documenta aquí.
  2. Los tokens v2 existentes con `cus_…` sin prefijo siguen siendo válidos y se interpretan como Stripe.
  3. No se acepta un id de cliente o de suscripción enviado por el navegador como prueba.

## 2. Estados de acceso (neutros respecto del proveedor)

El backend reduce el estado del proveedor a uno de estos valores; el frontend y `/analyze` solo dependen de estos.

| Estado | Proveedor (Stripe hoy; Paddle equivalente) | `/analyze` | Frontend |
|---|---|---|---|
| `active` | `active`, `trialing` | permitido | normal |
| `payment_required` | `past_due` (todos los proveedores, decisión propia) | **402** "Payment failed…" | aviso y botón para gestionar el pago de la suscripción existente; nunca ofrecer otra suscripción |
| `inactive` | `canceled`, `unpaid`, `incomplete`, `incomplete_expired`, `paused`, o ninguna suscripción | **403** | ofrecer suscripción |
| sin sesión válida | token ausente, caducado o revocado | **401** (texto según el caso) | iniciar sesión o recuperar el acceso |
| desconocido | el proveedor o el almacén no responden | **503** | "inténtelo más tarde"; **nunca** se trata como cancelación |

El acceso se obtiene solo de webhooks con firma verificada o de lecturas de la API del proveedor comprobadas contra
el cliente, el producto y el precio configurados. Nunca de redirecciones del navegador.

## 3. Esquemas de respuesta

**Errores** (todos los endpoints): `{"detail": "<texto>"}` con 400, 401, 402, 403, 413 o 503. El frontend muestra
`detail` y distingue por código, nunca por el texto. Excepción conocida: el frontend actual borra el token si el
texto contiene "subscription"; Cursor puede sustituirlo por la comprobación del código 401.

**`GET /billing/status`** (Cursor): hoy devuelve `product`, `amount_usd`, `founder_code`, `interval`,
`checkout_required`, `stripe_configured`, `access_model`, `recovery_available` y `new_checkout_available`. Al añadir
Paddle: campo `provider` (`"stripe"` | `"paddle"`) y `access_model` con un valor nuevo. Los campos existentes se
conservan mientras el frontend en producción los lea.
**Precio:** 150 USD/mes; 50 % de descuento solo el primer mes. Quedan restos de 100 USD y `FOUNDER80` en
`billing.py` (`AccessResponse.amount_usd = 100`, `FOUNDER_PROMO_CODE`), `paddle_webhooks.py`
(`_PARTNER_PLAN_USD = 100`) y en los textos del frontend en `main` ("$100 USD / mes"). Los corrige Cursor; nadie debe
configurar 100 USD ni el 80 %.

**`GET /billing/me`** (Cursor): `status`, `access`, `payment_required`, `has_subscription`, `cancel_at_period_end` y
`current_period_end`; se añade `provider`.

**`POST /analyze`** (Claude): `filename`, `truncated`, `contract_text`, `summary`, `document_title`, `metadata`,
`omissions`, `risks` e `integrity`, más `model_summary_supported`.
- `metadata` incluye `contract_type`, `parties`, `contract_date`, `fees`, `term`, `renewal` y `notice_period`; cada
  valor puede ser "No especificado" o "No determinado".
- Cada ficha de `risks` lleva los campos anteriores más `severity_reason`, `proposal_basis`, `related_passages`,
  `verification_notes`, `location` y `page_verified`.
- `integrity` incluye `status` (`complete` | `incomplete` | `unreliable`), `status_label`, `message` y `limitations`
  (lista de frases), además de los contadores anteriores.
Desde la candidata 2, `rescued_findings` es una lista aparte de fichas del auditor que la revisión descartó sin motivo específico: `article`, `excerpt`, `risk`, `interpretation`, `discarded_by`, `discard_reason` (o null), `status` (siempre "Pendiente de revisión humana; no validado por el sistema"), `source_desk` y `location`. No son hallazgos confirmados. Todos los campos nuevos son aditivos y el frontend tolera su ausencia. El acceso a `/analyze` lo decide la
dependencia `require_subscription` (de Cursor); el manejador (de Claude) no conoce proveedores.

## 4. Archivos asignados

| Agente | Backend | Frontend |
|---|---|---|
| **Cursor** | `app/billing.py`, `app/auth.py`, `app/sessions.py`, `app/store.py`, `app/subscriptions.py`, `app/paddle_webhooks.py`, `app/webhooks.py`, `app/signup.py`, `app/recovery.py`, `app/mail.py`, `app/availability.py`, `app/partners.py`, archivos nuevos `app/paddle*.py`, y sus pruebas (`test_paddle_*`, `test_subscription_access`, `test_sessions_recovery`, `test_shared_store`, `test_checkout_*`, `test_stripe_*`, `test_billing_*`) | páginas y componentes de suscripción, acceso y cuenta (`app/subscribe`, `app/success`, `app/cancel`, recuperación), funciones de facturación de `lib/api.ts` |
| **Claude** | `app/auditor.py`, `app/report_checks.py`, `app/rescue.py`, `app/extract.py`, `app/compare.py`, `app/playbook.py`, `eval/`, `test_report_checks.py`, `test_auditor_local_controls.py`, `test_integrity_status.py`, `test_excerpt_fallbacks.py`, `test_openai_errors.py`, `test_rescue.py`, `test_prompt_freeze.py`, `test_fixtures/` | `app/auditor/page.tsx`, `lib/reportPdf.ts`, `lib/reportView.ts`, tipos `AuditResponse`/`RiskFinding`/`AuditIntegrity`/`ContractMetadata` de `lib/api.ts` |
| **Compartidos** | `app/main.py` (Cursor: `require_subscription` y routers; Claude: manejadores `/analyze` y `/api/contracts/compare`), `app/config.py` (cada uno añade solo sus variables), `requirements.txt` (ninguno añade dependencias sin autorización del usuario) | `lib/api.ts` (por secciones, como arriba), `lib/copy.ts` (Claude solo las claves del informe) |

Reglas para los compartidos: cambios pequeños, dentro de su sección, anotados en el registro de abajo; quien
integre después hace rebase y resuelve. Ninguno reescribe la pila de PRs, hace push, merge o deploy sin autorización
del usuario, ni toca los cambios sin commit de `C:\Users\josue\LexShield\backend` ("beta guards").

## 5. Ramas y worktrees actuales (Claude)

`fix/auditor-local-controls`, `eval/auditor-real-calls`, `fix/openai-quota-honest-error` y
`feat/simulated-analysis-for-tests` (backend); `fix/report-evidence-and-states` y `feat/simulated-analysis-banner`
(frontend). Están en `C:\Users\josue\lexshield-worktrees\`, en local y sin push. Ninguna toca archivos de Cursor salvo
`app/main.py` (el manejador `/analyze`: informe de páginas PDF y ubicaciones).

## 6. Candidata integrada del auditor y cómo encaja el trabajo de Paddle

Ramas locales, sin push:
- backend `integration/auditor-candidate-1` (`971588d`, worktree `backend-integration`) = `origin/main` +
  `fix/openai-quota-honest-error` + etiqueta `auditor-candidata-1` + `fix/test-cors-order-independent`. 154 pruebas en
  verde.
- frontend `integration/report-candidate-1` (`021d2bd`, worktree `frontend-integration`) = `origin/main` +
  `fix/report-evidence-and-states`. `tsc`, `eslint` y `next build` correctos.

La candidata **no incluye la pila de facturación**. `/billing/status` todavía anuncia `amount_usd: 100` y
`founder_code: FOUNDER80`, que vienen de `main`. Corregirlo es tarea de Cursor dentro de su rama.

Para integrar Paddle encima de la candidata, con `git merge-tree` ya comprobado:
- con la cima de la pila (`feat/email-verified-checkout`) solo choca `app/main.py`, en los `import`: se conservan
  `pdf_page_report` y los routers `recovery` y `signup`;
- `fix/test-cors-order-independent` cambia `_allowed_origins` en `app/main.py`; si Cursor toca CORS, debe partir de
  esa versión.

**No hacer llamadas a OpenAI** en las pruebas de Paddle: el usuario las ha pausado para conservar saldo. Para
recorrer el flujo de pago con análisis, la rama `feat/simulated-analysis-for-tests` (`9557ba7`) ofrece
`ANALYSIS_BACKEND=simulated`. Solo se activa con un `ENVIRONMENT` explícito y sin claves en vivo. En el frontend
existe el banner correspondiente (`feat/simulated-analysis-banner`).

Lo que Claude **no** tocará mientras Cursor trabaja: todo lo asignado a Cursor en la sección 4, y la sección de
facturación de `lib/api.ts` y `lib/copy.ts`.

## Registro de cambios del contrato

- 2026-10-30 · Claude · versión inicial.
- 2026-10-30 · Claude · sección 6: candidata integrada del auditor, conflicto conocido en `app/main.py`, pausa de
  llamadas a OpenAI y modo simulado para las pruebas de pago.
- 2026-10-30 · Claude · candidata 2: `rescued_findings` en `/analyze`, `app/rescue.py`; ramas `integration/auditor-candidate-2` (`5322124`, 168 pruebas) e `integration/report-candidate-2` (`c4319f5`). `app/main.py` (`_review_with_critic`) pasa las fichas del auditor al supervisor.
