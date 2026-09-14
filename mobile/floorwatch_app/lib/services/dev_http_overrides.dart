import 'dart:io';

import 'package:flutter/services.dart' show rootBundle;

/// Trusts this dev machine's Norton Antivirus SSL-inspection root CA, for
/// Dart's own networking stack specifically (dart:io's HttpClient/
/// SecureSocket, which is what package:http's IOClient uses on mobile).
///
/// This is NOT the same thing as Android's network_security_config.xml —
/// that mechanism covers Android's native HTTP stack (WebView, Java
/// HttpURLConnection) but has NO effect on Dart's independent TLS
/// implementation, which is why an earlier attempt at this fix via
/// AndroidManifest.xml's networkSecurityConfig did nothing — the exact
/// same CERTIFICATE_VERIFY_FAILED / "unable to get local issuer
/// certificate" error from package:http still occurred with that XML in
/// place. Dart's networking needs its OWN trusted-root override instead.
///
/// Applied ONLY when compiled in debug mode (see main.dart's kDebugMode
/// guard) — this must never run in a release build a real user installs.
/// A real user's phone, not running this same local SSL inspection on
/// this same machine, would never need or want this override.
class _DevTrustingHttpOverrides extends HttpOverrides {
  final SecurityContext _context;
  _DevTrustingHttpOverrides(this._context);

  @override
  HttpClient createHttpClient(SecurityContext? context) {
    return super.createHttpClient(_context);
  }
}

/// Loads the bundled Norton root CA (assets/norton_root_ca.pem, added to
/// pubspec.yaml) into a SecurityContext and installs it as the global
/// HttpOverrides. Call once, early in main(), before any network call.
/// Swallows its own failure (e.g. asset missing on a build that stripped
/// it) rather than crashing app startup over a dev-only convenience —
/// networking then just falls back to the platform's default trust
/// store, which is exactly production behavior anyway.
Future<void> installDevCertificateOverride() async {
  try {
    final bytes = await rootBundle.load('assets/norton_root_ca.pem');
    final context = SecurityContext(withTrustedRoots: true);
    context.setTrustedCertificatesBytes(bytes.buffer.asUint8List());
    HttpOverrides.global = _DevTrustingHttpOverrides(context);
  } catch (_) {
    // No-op — see doc comment above.
  }
}
