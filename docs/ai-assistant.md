# Nong ib: Support AI assistant

## Delivered first version

The assistant is named Nong ib. Its panel, launcher name badge, greeting, suggested questions, status and error messages use Thai. Thai is the default reply language; an explicit request for another language is respected. Actual infrastructure menu labels stay accurate in replies.

The console includes a floating elephant launcher and chat panel (click the doll to open; click again to collapse), also reachable from Settings → Support → Open AI Assistant. Administrators enable the assistant inside the panel. All signed-in roles may ask documentation questions. Optional context is off by default and can be previewed before sending. Context uses the user's existing permissions: VM counts, node health and recent backup-job status are allowlisted; credentials, full configuration, raw logs and disk contents are not automatically collected.

The first version provides eight curated, version-specific reference topics. It is not a complete repository-document retrieval service. The model has no tools, command executor or infrastructure mutation capability. It produces advice only. Messages render as plain text. History stays in tab memory; the latest eight turns are sent with each request and are cleared by New chat or reload. The gateway persists usage counters, not conversations. Proxy logging must likewise avoid bodies and authorization headers.

## Architecture and subscription enforcement

Browser → authenticated ibHiCS Manager → company AI gateway → Claude Messages API.

The Manager verifies the existing signed Support license and its expiry, cluster identity and licensed node count for each interaction. AI is disabled by default. A locally edited checkbox cannot grant a Support subscription.

The company gateway independently verifies the license, enrollment token, enrolled cluster and node count before calling the provider. A per-installation daily request allowance is reserved atomically in SQLite, including failed provider attempts. Limits survive service restarts. Requests have bounded message/history/body sizes and a maximum 1,200 output tokens. The model is configured by the company, not by customer requests. Daily requests are a usage allowance, not a guaranteed dollar spend limit; choose the model, allowance and provider billing limits together.

The provider API key exists only on the company gateway host. Customer Managers receive a separate enrollment credential; browsers receive neither credential. Support access does not require a customer Claude login. A signed license is not a substitute for enrollment.

## Company gateway setup (not yet provisioned in the acceptance preview)

1. Prepare a company-controlled HTTPS endpoint with a reverse proxy. The gateway process binds to loopback. Do not deploy the gateway on a customer's Manager with the company's provider key.
2. Choose a supported model and configure `ANTHROPIC_API_KEY` and `IBHICS_AI_MODEL` using the service's private environment file or secret manager. Do not put real keys in source control, command history, URLs or chat.
3. Enroll each purchased installation on the gateway host:

```sh
python -m ibhics.ai.enroll --profiles /var/lib/ibhics-ai/profiles.json --token-file /var/lib/ibhics-ai/customer-installation.token --cluster-id IBHX-CLUSTER-ID --nodes 2 --daily-requests 50
```

The helper writes a private token file and stores only its hash in the profiles file. It refuses accidental replacement. Deliver the enrollment credential privately to the matching Manager.

4. Run behind HTTPS:

```sh
ibhics-ai-gateway --profiles /var/lib/ibhics-ai/profiles.json --usage /var/lib/ibhics-ai/usage.sqlite --port 8891
```

The source checkout also supports `python -m ibhics.ai.gateway` with the same arguments. If the license signer uses an operator-managed public key, supply `--trust-dir /path/to/company-trust`; it must contain `release-public.pem`. The private signing key never belongs on the gateway or customer node.

5. Configure the customer Manager service with `IBHICS_AI_GATEWAY_URL=https://your-company-ai-host/v1/chat` and `IBHICS_AI_GATEWAY_TOKEN` from that installation's private enrollment file. Configure both Manager nodes consistently before enabling AI. No secret is entered in the browser. The enabled flag uses a private `assistant.json` next to `settings.json` and follows shared Manager state across handover.
6. Apply a valid signed Support license. An administrator opens Settings → Support → AI Assistant and enables AI. Test a documentation question, then preview optional context before sending it.

To revoke an installation independently of license expiry, set its gateway profile `enabled` to false and restart/reload the gateway process. The initial CLI reads profiles at startup. Subscription expiry is checked on every request without needing a restart. Preserve the usage database across restarts. This first version supports one gateway deployment using one local SQLite database; separate replicas with independent databases would each have their own allowance and are not a supported deployment topology.

## Privacy and practical limits

The interface says messages are processed by an external AI service. Common token, authorization-header, password-assignment and private-key patterns receive best-effort redaction before forwarding; this cannot guarantee detection of every secret. Users should not paste secrets. Resource names and job status may still be sensitive, so automatic system context is opt-in. Review the company's privacy terms and provider configuration before offering the service to customers.

Internet connectivity is required for AI replies. No provider credential or hosting endpoint was supplied during implementation, so the website preview and integration tests use simulated providers; no real Claude response or live provider billing has been verified. No production AI gateway was deployed or enabled. Infrastructure remains unchanged by chat.

## Responsive mascot behavior

The mascot keeps the original 3D-rendered plush image and uses CSS perspective, a soft floating shadow, gentle bobbing and a hover wiggle. It is an animated image, not a volumetric 3D mesh that can be rotated from arbitrary angles. Reduced-motion preferences disable the animation. The panel opens on demand and is clamped using the visual viewport, including resize/zoom and mobile keyboard viewport changes. The same doll toggles the panel closed.

## Visual assets

`ibhics/manager/static/ai-elephant.png` is the original transparent mascot generated with the built-in imagegen tool. Preview screenshots are in `deliverables/ai-elephant-floating-closed.png` and `deliverables/ai-elephant-floating-chat.png`. The local preview uses simulated infrastructure and visibly simulated replies. Prompt and asset metadata are in `docs/ai-mascot-design.json`.
