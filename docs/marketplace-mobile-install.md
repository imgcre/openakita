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

## Task recovery

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
