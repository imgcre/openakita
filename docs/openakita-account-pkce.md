# OpenAkita Account login integration

OpenAkita Desktop uses the public OIDC client `openakita-desktop`. It never has
a client secret. The backend opens a temporary loopback listener on
`127.0.0.1:1455`, generates a fresh state and S256 PKCE verifier, and returns
the Account authorization URL to Setup Center.

```text
GET  /api/account/capability
POST /api/account/login/start
POST /api/account/login/cancel/{attempt_id}
GET  /api/account/login/status/{attempt_id}
GET  /api/account/status
POST /api/account/entitlements/refresh
POST /api/account/logout
```

The desktop registered redirect URI is
`http://127.0.0.1:1455/auth/callback`. Refresh tokens are stored only in the OS
credential store through `keyring`; there is no plaintext file fallback.
Access tokens stay in process memory. A successful login fetches `/oauth/userinfo`
and `/api/v1/me/entitlements`, then persists only the identity and entitlement
read model in `data/account_identity.db`.

Feature gates must evaluate the cached entitlement status and expiry at read
time. A central `suspended` event revokes Account sessions in that database and
all Account-backed operations fail immediately. The existing local web password
remains a separate break-glass path and is not revoked by Account suspension.

Setup Center's sidebar account menu drives these endpoints, polls the loopback
attempt, shows the cached account/entitlement status, and lets the user refresh
entitlements. Logout clears the local OS credential without opening another
browser tab.

## Remote Web sign-in (RFC 8628)

Web browsers (including mobile browsers) and desktop connections to a remote backend use the standard
[OAuth Device Authorization Grant](https://www.rfc-editor.org/rfc/rfc8628).
Local desktop connections retain Authorization Code + S256 PKCE. All Web builds
use device authorization, including localhost URLs that might be SSH tunnels.

1. OpenAkita requests a short-lived grant from `POST /oauth/device_authorization`
   using the public `openakita-desktop` client. No client secret or redirect URI
   is sent.
2. The Web UI first displays the user code in a confirmation panel (a bottom
   sheet on mobile). Only clicking **Continue to account center** opens
   `verification_uri_complete` in a new tab; preparation never opens a blank tab.
   The panel keeps the same code and request when reopening or retrying a blocked
   popup. Expired requests must be restarted. The code is already filled in at
   the account center. The user signs
   in, checks that both codes match, and explicitly confirms the requesting app,
   account and scopes. Visiting the link alone never approves a request.
3. The OpenAkita backend polls `POST /oauth/token` with grant type
   `urn:ietf:params:oauth:grant-type:device_code`. The original UI polls only its
   own backend and updates automatically when authorization succeeds.

After the user confirms, the account page attempts to close its own login tab.
Browsers that prohibit automatic closing keep a return button and instructions
for returning manually. When OpenAkita regains visibility/focus or is restored
from browser history, it immediately checks the pending attempt. These checks
are serialized and still honor the backend's provider polling interval. The
account page reports authorization completion; OpenAkita reports successful
sign-in only after it has received and stored the credentials.

The account center returns a ten-minute expiry and an initial five-second poll
interval. OpenAkita honors `authorization_pending`, increases the interval by
five seconds on `slow_down`, and uses exponential backoff for network timeouts
and temporary server errors. Denial, expiry and consumed grants are terminal.
Cancelling closes the tab and stops backend polling; unused account-center
grants expire automatically. Backend restarts require starting a new attempt.

The device code stays in backend memory. Only the user code, verification URLs
and attempt status reach the OpenAkita browser. Access tokens stay in memory;
refresh tokens use the existing OS credential store. Starting, polling and
cancelling login remain behind normal OpenAkita Web access authentication.
There is no public callback route, callback proxy or per-deployment redirect
registration. The backend needs outbound access to the account center; the
account center does not need inbound access to the self-hosted deployment.

Deploy the updated **openakita-account first**, then OpenAkita. Account schema
migration 7 adds the short-lived device authorization table without modifying
existing clients, sessions or credentials. The account center currently permits
device grants only for active public first-party platform clients (including the
seeded `openakita-desktop`), and preserves scope policy, refresh rotation, reuse
detection and browser-session revocation. Custom providers must implement the
same RFC 8628 endpoints. Older providers show an upgrade-required error instead
of falling back to the unreachable loopback callback.

If login cannot start, check account-service availability/version and popup
permissions. If authorization succeeds but local sign-in fails, check backend
connectivity and OS credential-store availability. The existing `keyring`
requirement also applies to server deployments; there is no plaintext fallback.

## Native mobile sign-in (Authorization Code + S256 PKCE)

Capacitor builds use `flow: native`, not the browser device-code dialog. The
instance creates the state/verifier and an expiring attempt; the App opens the
authorization URL through the local `@openakita/native-auth` plugin. Android
uses AndroidX Auth Tab with automatic Custom Tabs compatibility. iOS uses
`ASWebAuthenticationSession`. Account pages are never loaded in the App WebView.

The existing public `openakita-desktop` client remains the **instance's** client
identity, preserving existing refresh-token storage, rotation, and product
integrations. Its display name is now OpenAkita and its registered redirect
allowlist includes these project-owned mobile callbacks:

- Android release: `https://account.openakita.cn/oauth/mobile/callback`.
- Android debug and iOS: `com.openakita.mobile:/oauth/callback`.

The account center publishes `/.well-known/assetlinks.json` with the production
APK signing certificate, verified against the official v1.27.38 APK. During
signing-key rotation, publish both old and new SHA-256 fingerprints. Never add
debug signing keys to the production association. iOS uses a system-session
callback scheme to support iOS 15+ without requiring Universal Links setup.

The App validates the exact callback URI, state and unambiguous code/error,
then POSTs those fields to the **original instance** at
`/api/account/login/native/callback/{attempt_id}` using its existing instance
authentication. This endpoint is protected; it is not a public OAuth redirect.
The backend verifies its own state and PKCE, redeems the code once, persists
the refresh token in its existing OS vault, and returns only status. Repeated
delivery of a successful native callback is idempotent. Server switching aborts
delivery and prevents account-status publication into a different server view.

Only attempt routing metadata is stored in App localStorage, never the verifier,
authorization code, or account-center tokens. Native activity/session callbacks
retain results until acknowledged; a transient delivery failure can retry the
same attempt. Android activity restoration handles a returned browser result
after process recreation. If the OS discards the entire session, or the backend
restarts/expires the attempt, start a new login. Closing system auth cancels the
pending backend attempt. A callback page fallback explains how to enable App
Links when the OS does not deliver a verified HTTPS link.

Build/sync with `npm run build:cap` and `npx cap sync android` (or `ios`). The
local plugin includes both Android and iOS implementations; the existing iOS
workflow creates the Xcode project and discovers the plugin during Capacitor
sync. Publish the updated account center before distributing the updated App.
Custom account providers must allow the same exact mobile redirects on their
configured public client; mobile authorization URLs must use HTTPS.

## Distribution configuration

Account integration is a distribution-level capability with three modes:

```text
OPENAKITA_ACCOUNT_MODE=openakita  # official hosted service (default)
OPENAKITA_ACCOUNT_MODE=custom     # OEM identity service
OPENAKITA_ACCOUNT_MODE=disabled   # no account routes, credentials, or account UI
```

The official mode uses the hosted service by default:

```text
OPENAKITA_ACCOUNT_BASE_URL=https://account.openakita.cn
OPENAKITA_ACCOUNT_CLIENT_ID=openakita-desktop
```

Override `OPENAKITA_ACCOUNT_BASE_URL` only when testing against a local Account
service. Custom mode requires an explicit `OPENAKITA_ACCOUNT_BASE_URL` and
`OPENAKITA_ACCOUNT_CLIENT_ID`; `OPENAKITA_ACCOUNT_DISPLAY_NAME` and
`OPENAKITA_ACCOUNT_PROVIDER` customize the provider identity shown to users.
Provider credentials use separate OS-vault slots, so an OEM token is never sent
to the official service or another custom provider.

When account mode is disabled, only `GET /api/account/capability` remains
mounted so the frontend can render an account-free application menu. OAuth,
status, entitlement, logout, and status-propagation routes are absent, and
startup clears locally stored account refresh tokens. Core local OpenAkita
features continue to work without an account.

An account-enabled always-on server may additionally expose the signed D26 receiver at
`POST /api/internal/openakita/users/status`. Desktop-only processes must not be
configured as Account Outbox targets because they have no stable ingress.
