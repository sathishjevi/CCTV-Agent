import 'package:flutter/foundation.dart' show kDebugMode;
import 'package:flutter/material.dart';

import 'services/dev_http_overrides.dart';
import 'services/push_service.dart';
import 'services/token_storage.dart';
import 'screens/phone_entry_screen.dart';
import 'screens/supervisor_home_screen.dart';
import 'screens/task_list_screen.dart';

void main() async {
  // kDebugMode-gated: trusts this dev machine's local SSL-inspection CA
  // for Dart's own networking only in debug builds — see
  // dev_http_overrides.dart's doc comment for why this exists and why
  // it's structurally impossible for it to affect a release build.
  if (kDebugMode) {
    WidgetsFlutterBinding.ensureInitialized();
    await installDevCertificateOverride();
  }
  runApp(const FloorwatchApp());
}

class FloorwatchApp extends StatelessWidget {
  const FloorwatchApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Floorwatch',
      scaffoldMessengerKey: PushService.messengerKey,
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: const Color(0xFF2F6FED), // matches the web dashboard's accent blue
      ),
      home: const _StartupGate(),
    );
  }
}

/// Decides where to land on cold start: already logged in -> task list,
/// otherwise -> phone entry. A stored token isn't re-validated here (an
/// expired/revoked one just 401s on the first API call, and every
/// screen already handles ApiException) — this is purely "skip the
/// login screen if we plausibly don't need it."
class _StartupGate extends StatefulWidget {
  const _StartupGate();

  @override
  State<_StartupGate> createState() => _StartupGateState();
}

class _StartupGateState extends State<_StartupGate> {
  bool _checked = false;
  bool _loggedIn = false;
  String _role = 'employee';
  String _kind = 'employee';

  @override
  void initState() {
    super.initState();
    _check();
  }

  Future<void> _check() async {
    final token = await TokenStorage.instance.readToken();
    String role = 'employee';
    String kind = 'employee';
    if (token != null) {
      role = await TokenStorage.instance.readRole() ?? 'employee';
      kind = await TokenStorage.instance.readKind();
      // Best-effort — see push_service.dart's docstring for what's
      // required before this actually delivers anything.
      // Push is per-employee-device; a dashboard-account session has no
      // employee identity to register a token against.
      if (kind == 'employee') {
        try {
          await PushService.instance.initialize();
        } catch (_) {}
      }
    }
    if (!mounted) return;
    setState(() {
      _loggedIn = token != null;
      _role = role;
      _kind = kind;
      _checked = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_checked) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    if (!_loggedIn) return const PhoneEntryScreen();
    if (_kind == 'dashboard') return const SupervisorHomeScreen(dashboardLogin: true);
    return (_role == 'supervisor' || _role == 'admin') ? const SupervisorHomeScreen() : const TaskListScreen();
  }
}
