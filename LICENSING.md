# Licensing — offline, zero-telemetry Pro keys

Tera Pilot Pro (and future paid tiers) is gated behind a **signed license
key that verifies entirely offline**. This is a deliberate consequence of
the project's no-telemetry constraint (see `THREAT_MODEL.md`): there is no
license-check network call, no phone-home, and no usage reporting tied to
the key. The test suite enforces this — `socket.socket` and `urllib` are
monkeypatched to raise if any network call is attempted during activation
or feature checks.

## The model

- The **seller** holds an Ed25519 private signing key, offline, outside
  this repository.
- The **public key** ships embedded in the package
  (`tera_pilot/license_pubkey.pem`), so every client can verify signatures
  without any network access.
- A license is a signed JSON payload:

  ```json
  {
    "customer_id": "usr_abc123",
    "tier": "pro",
    "issued_at": "2026-08-17T00:00:00Z",
    "expires_at": "2027-08-17T00:00:00Z",
    "features": ["second_opinion", "cost_router", "spend_dashboard"]
  }
  ```

  `expires_at` may be `null` for a non-expiring key. The signature covers
  the canonical JSON (sorted keys, no whitespace); the encoded form is
  `base64url(payload).base64url(signature)`.

Key issuance is a **seller-side step** and is intentionally NOT part of the
shipped code: there is no generation/sales backend in this repository. The
module only verifies.

## Redeeming a purchased key

A customer who bought a key runs:

```bash
tera-pilot license activate <license-key>
tera-pilot license status       # exit 0 = valid, 1 = invalid/absent
tera-pilot license deactivate
```

Activation verifies the signature against the embedded public key, checks
`expires_at` against the local clock, and persists the key to
`~/.tera_pilot/license.json` (the same directory convention as
`audit_key`). `status` re-verifies the persisted key on every read, so an
expired or tampered file flips to `valid: false` without re-activation.

## Failure mode — fails closed

Invalid, expired, missing, or tampered license → the Pro feature **falls
back to the free tier**. Nothing crashes; gated surfaces return an explicit
"Pro license required" response (e.g. `error: pro_required` on
`/api/second_opinion/run`, `/api/spend/report`; `LicenseRequiredError` on
cost-router config writes). There is no code path where a non-Pro user can
reach a gated feature.

## Local development

`TERA_PILOT_PRO=1` (also `true`/`yes`/`on`) enables all Pro features
without a license. **This is a local-dev override only — never use it in
production.** It is documented as such in code and in
`tera_pilot/licensing.py`.

`TERA_PILOT_LICENSE_PUBKEY` points the verifier at an alternate Ed25519
public key (PEM). Used by the test suite (which mints throwaway keypairs)
and by the seller for key rotation. Same trust level as the dev override —
test/dev only.

## Issuing keys (seller side, v2.3.6 CLI)

Since v2.3.6 there is a seller-side CLI for issuing keys — fully offline,
zero telemetry (no network call is ever made; the private key never leaves
your machine). Do this ONCE, on an offline machine, and keep the private
key there:

```bash
# 1. Generate a keypair (private key written with 0600 perms)
tera-pilot license gen-keypair --out ~/seller-keys
#    -> ~/seller-keys/license_priv.pem    (KEEP OFFLINE — never ship/commit)
#    -> ~/seller-keys/license_pubkey.pem  (embed this as tera_pilot/license_pubkey.pem)

# 2. Issue a license for one customer (paste the printed key into your
#    email / license page — it is self-contained and needs no backend)
tera-pilot license issue \
    --private-key ~/seller-keys/license_priv.pem \
    --customer usr_abc123 \
    --tier pro \
    --expires 2027-08-17T00:00:00Z \
    --features second_opinion,cost_router,spend_dashboard
```

`--expires` is optional (omit for a non-expiring key). The issued license
string is a single line — a customer pastes it into
`tera-pilot license activate <key>` and it verifies entirely offline
against the embedded public key. Because verification never phones home,
you can distribute keys through any channel (email, Gumroad/LemonSqueezy
fulfilment, a PDF, a pastebin) without running any license server.

The same operations are available as Python helpers
(`generate_keypair()`, `sign_payload()`, `issue_license()`) for tests and
custom issuance tooling; they are not part of the client verification path.

When you rotate the embedded public key (new keypair + repackage),
previously issued keys stop verifying — keep the keypair stable for as
long as you want existing customers to work.

## What we are deliberately NOT claiming

- **No revocation list.** A leaked license stays valid until it expires.
  Rotation is a seller-side concern (short expiries, re-issuing).
- **Local clock is trusted** for expiry checks. A user can shift their own
  clock; this is the same trust boundary as the rest of the local-first
  app and is documented in the threat model.
- **No key-management ceremony** (HSM, secure enclave). The seller's
  private key is protected by process discipline, not hardware.
- **Not an anti-piracy system.** Signatures prove the key came from the
  seller; they do not stop a user from sharing their key. The same
  trade-off as most offline licensing.
