# Installing Marketplace resources from the mobile app

The Android app opens the existing Marketplace in a system browser tab. After
acquisition or purchase, Marketplace returns an installation instruction to the
app. The app shows the selected server, resource, version, permissions and
dependencies. Only explicit confirmation starts installation on that server.

## Target and identity

Opening Marketplace records the active server ID, URL, display name and a random
state locally. Only the state, backend version and return mode reach Marketplace;
server addresses and connection credentials are not sent to the website.
Marketplace stores this context per browser tab, with an eight-hour lifetime.
The app reads the selected backend's health/version when opening Marketplace,
independently of the startup version check. Failed connections can be retried,
and switching servers during this request cancels opening the stale context.

An installation return must match an unexpired local context and the trusted
Marketplace origin. Switching servers does not retarget an installation. The app
offers connection management so the user can reconnect to the original server.
Each request binds the original server credential and rejects redirects. A late
response after a server switch cannot update the current installation UI.
Installation routes accept either the local desktop credential or a validated
explicit instance Bearer token. The desktop browser handoff route still requires
the local desktop credential and cannot be used with the mobile instance token.

Desktop and mobile use the same installation progress and plugin permission
components. Download percentages reflect measured bytes; dependency operations
show the active stage, package and elapsed time with indeterminate progress.

The backend requests `/oauth/desktop-install-proof` from Account, using its stored
refresh token. Despite its historical name, this existing protocol also supports
the instance account logged in from mobile. The short-lived proof is bound to the
instruction, Marketplace client and backend device ID. Marketplace checks that
its subject owns the resource. Confirmation obtains a fresh proof and rechecks
authorization, so changing accounts after preview does not authorize installation.
Account access and refresh tokens never enter the installation link or app UI.

## Android return links

- Signed release: `https://marketplace.openakita.cn/openakita/install#...`.
  Marketplace publishes `/.well-known/assetlinks.json` for the official APK
  signing certificate. Keep the certificate synchronized during key rotation.
- Debug/browser fallback: `com.openakita.marketplace://marketplace/install#...`.
  The installation receiver is separate from the OAuth callback receiver.
- The fragment contains the short-lived installation ticket, state and trusted
  Marketplace origin. It is not sent in HTTP requests. The fallback page clears
  the fragment from the address bar and offers an explicit return button.
- Both `appUrlOpen` and `getLaunchUrl` are handled, covering running and cold apps.

If the browser stays on the return page, tap **Open OpenAkita** and permit the
browser to open the app. No clipboard or manually pasted callback is required.
The app needs an existing valid Marketplace context; an unrelated external link
cannot silently choose a server.

## Remote Web installation

The Web interface opens Marketplace with `client=web`. Mobile browsers navigate
in the same tab. Desktop browsers open a new tab, keeping the original OpenAkita
page available. On return, a lightweight handoff page delivers the request to
the originating page, which shows the existing installation confirmation.
The new tab receives its own session context while still same-origin, then drops
its opener before navigating to Marketplace. If popups are blocked, navigation
falls back to the current tab. No cross-origin opener is retained.

Desktop handoff uses a same-origin BroadcastChannel and a registry of sessions
in the originating tab's sessionStorage. Each page runtime has a fresh random
instance ID: the live originating instance takes precedence over a duplicated
tab, while a refreshed source can offer its persisted session. The return page
selects exactly one responder. IndexedDB serializes receipt claims, including
fallback claims, so a delayed delivery cannot race a local installation. A
missing acknowledgment after a claim prompts retry instead of silently installing
in another tab. Accepted requests are persisted and queued in the source tab;
closing the current dialog advances to the next confirmation. Preparation and
installation still require the normal authentication and explicit confirmation.

If no eligible source responds (closed, suspended or connected elsewhere), the
returning page continues the existing local confirmation flow. Unsupported
BroadcastChannel/IndexedDB environments use that same local flow. After receipt
is acknowledged, the source tries to focus and the return page tries to close;
browser restrictions leave a clear instruction to switch to the original tab.
Marketplace itself needs no protocol or deployment change for this handoff.
Its per-tab context lasts 30 minutes and contains a random state, the original
Web page and the target API base. Web targets always use the page's origin,
independently of native desktop connection state. Marketplace receives only the clean Web return
address (origin and path), state and version. The original query/hash and instance
credentials stay in OpenAkita's origin. State generation uses `getRandomValues`,
which is available on LAN HTTP pages as well as HTTPS pages.

After acquisition, both resource details and the resource library return to that
Web address with a one-use instruction in the URL fragment. OpenAkita captures
and clears the fragment before routing or login. It validates the state, expiry,
page, Marketplace origin and current API target, then uses the existing
authenticated prepare/confirm APIs. Preparation never confirms installation.
After preparation, reload recovery uses the job ID; failed preparation keeps the
ticket for retry. Progress, background tasks and plugin authorization share the
desktop/App components. The Marketplace page never fetches a private backend or
receives its login credentials.

This Web path requires the existing remote instance login and matching instance
and Marketplace account identities. It does not grant browser cookies or local-IP
exemptions access to native desktop credentials. Expired, unrelated or replayed
returns require reopening Marketplace from OpenAkita. Entering Marketplace from
a desktop client clears a previous Web/App context in that browser tab.

Web returns use the dedicated `/web/marketplace-return` shell. The backend serves
this shell, the normal Web entry and the service worker with `Cache-Control:
no-store`, so a return navigation cannot reuse an older cached entry document.
The original page path, query and fragment are restored locally after capture.
Hashed static assets retain their normal caching behavior. Reverse proxies must
forward this callback path to OpenAkita and honor the entry's cache policy.

Ship the updated OpenAkita backend together with the rebuilt Web assets and
restart the backend to enable this entry. The existing Marketplace accepts this
callback path; this handoff fix requires no Marketplace, Account or APK update.
Verify an HTTP LAN address and HTTPS reverse proxy, refresh before
confirmation, background completion/permission recovery and an expired link.

## App task recovery

The app persists the target and returned job ID. A response lost during preparation
can be retried with the same instruction. After preparation, subsequent operations
use the saved job ID instead of consuming the ticket again. Closing the progress view lets the
backend continue; tapping Marketplace reopens the pending task. Reopening the app
queries the job without confirming installation again. Backend restart failures
remain explicit and require a new installation attempt.

## Shipping and verification

Ship the Marketplace web changes, updated OpenAkita backend and new APK together.
This feature requires no Account deployment or database migration. It does not
export a remote instance owner's account into a Marketplace browser session;
the two account identities must agree to install acquired resources.

`app-debug.apk` uses the development signing key; `app-release-unsigned.apk`
must be signed with the official key before installing or distributing it.
Browser/unit tests cannot establish OS App Links verification on a real phone.
Check cold start, background return, cancelled authorization, multiple servers,
offline/reconnect and installation completion on a signed release device.

There is no generated iOS host project in this checkout. The shared Capacitor
installation UI and browser fallback can be reused there, but the iOS host must
register `com.openakita.marketplace` in `CFBundleURLTypes` and forward open-URL
events to Capacitor before it can be shipped. No iOS binary is produced here.
