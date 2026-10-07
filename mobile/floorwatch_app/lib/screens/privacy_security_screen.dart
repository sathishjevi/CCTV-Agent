import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../services/api_client.dart';
import '../services/token_storage.dart';
import 'mfa_dialogs.dart';
import 'privacy_notice_screen.dart';

/// "Privacy & security": read the privacy notice, get a copy of your data, make a
/// privacy request (correct / erase / object…), and manage two-step verification.
/// Staff signed in with a dashboard account (the "Admin login") are not
/// employees, so they only see the two-step section.
class PrivacySecurityScreen extends StatefulWidget {
  const PrivacySecurityScreen({super.key});

  @override
  State<PrivacySecurityScreen> createState() => _PrivacySecurityScreenState();
}

class _PrivacySecurityScreenState extends State<PrivacySecurityScreen> {
  bool _isEmployee = true;
  Map<String, dynamic>? _mfa;
  List<Map<String, dynamic>> _requests = [];
  String? _error;
  bool _loading = true;

  static const _kinds = {
    'access': 'See what you hold about me',
    'correction': 'Correct something',
    'erasure': 'Erase my data',
    'restriction': 'Limit how my data is used',
    'objection': 'Object to how my data is used / ask for a human to review a flag',
    'portability': 'Give me my data in a portable format',
    'other': 'Something else',
  };

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _isEmployee = (await TokenStorage.instance.readKind()) == 'employee';
      final mfa = await ApiClient.instance.mfaStatus();
      final requests = _isEmployee ? await ApiClient.instance.fetchMyPrivacyRequests() : <Map<String, dynamic>>[];
      if (mounted) {
        setState(() {
          _mfa = mfa;
          _requests = requests;
        });
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (_) {
      if (mounted) setState(() => _error = 'Network error — pull down to retry.');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _snack(String message) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));

  // ── privacy ──

  Future<void> _downloadMyData() async {
    try {
      final data = await ApiClient.instance.fetchMyDataExport();
      final json = const JsonEncoder.withIndent('  ').convert(data);
      await Clipboard.setData(ClipboardData(text: json));
      if (!mounted) return;
      final tasks = (data['tasks'] as List).length;
      final events = (data['activity_events'] as List).length;
      await showDialog<void>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Your data is on the clipboard'),
          content: Text('A copy of everything held about you — your details, $tasks task(s) and $events activity '
              'record(s) — is now on your clipboard as text. Paste it into an email or a notes app to keep it.\n\n'
              'It never includes your password or device key.'),
          actions: [FilledButton(onPressed: () => Navigator.pop(context), child: const Text('OK'))],
        ),
      );
    } on ApiException catch (e) {
      _snack(e.message);
    } catch (_) {
      _snack('Network error — check your connection and try again.');
    }
  }

  Future<void> _makeRequest() async {
    String kind = 'access';
    final details = TextEditingController();
    final send = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('Make a privacy request'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              DropdownButtonFormField<String>(
                initialValue: kind,
                isExpanded: true,
                decoration: const InputDecoration(labelText: 'What do you need?', border: OutlineInputBorder()),
                items: [for (final e in _kinds.entries) DropdownMenuItem(value: e.key, child: Text(e.value, overflow: TextOverflow.ellipsis))],
                onChanged: (v) => setDialogState(() => kind = v ?? 'access'),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: details,
                maxLines: 3,
                maxLength: 2000,
                decoration: const InputDecoration(labelText: 'Details (optional)', border: OutlineInputBorder()),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Send request')),
          ],
        ),
      ),
    );
    if (send != true) return;
    try {
      await ApiClient.instance.createPrivacyRequest(kind, details.text.trim());
      _snack('Request sent. You will get a response within the time stated in the privacy notice.');
      _load();
    } on ApiException catch (e) {
      _snack(e.message);
    } catch (_) {
      _snack('Network error — check your connection and try again.');
    }
  }

  // ── two-step verification ──

  Future<void> _enableMfa() async {
    final done = await runMfaEnrollment(
      context,
      begin: ApiClient.instance.mfaSetup,
      confirm: ApiClient.instance.mfaEnable,
    );
    if (done != null) _load();
  }

  Future<void> _newRecoveryCodes() async {
    final result = await promptForMfaCode<Map<String, dynamic>>(
      context,
      title: 'New recovery codes',
      message: 'Enter a current 6-digit code. Your old recovery codes will stop working.',
      verify: ApiClient.instance.mfaNewRecoveryCodes,
    );
    if (result != null && mounted) {
      await showRecoveryCodesDialog(context, (result['recovery_codes'] as List).cast<String>());
      _load();
    }
  }

  Future<void> _disableMfa() async {
    final password = TextEditingController();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Turn off two-step verification?'),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          const Text('Your account will be protected by your password alone. Confirm with your password, then a current code.'),
          const SizedBox(height: 12),
          TextField(
            controller: password,
            obscureText: true,
            decoration: const InputDecoration(labelText: 'Password', border: OutlineInputBorder()),
          ),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Continue')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    final done = await promptForMfaCode<bool>(
      context,
      title: 'Confirm with a code',
      verify: (code) async {
        await ApiClient.instance.mfaDisable(password.text, code);
        return true;
      },
    );
    if (done == true) {
      _snack('Two-step verification is off.');
      _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final mfa = _mfa;
    final enabled = mfa?['enabled'] == true;
    final required = mfa?['required'] == true;
    return Scaffold(
      appBar: AppBar(title: const Text('Privacy & security')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: [
          if (_loading && mfa == null) const Padding(padding: EdgeInsets.all(32), child: Center(child: CircularProgressIndicator())),
          if (_error != null)
            Padding(padding: const EdgeInsets.all(16), child: Text(_error!, style: const TextStyle(color: Colors.red))),
          if (_isEmployee) ...[
            const _Header('Privacy'),
            ListTile(
              leading: const Icon(Icons.description_outlined),
              title: const Text('Privacy notice'),
              subtitle: const Text('What we collect, why, and for how long'),
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const PrivacyNoticeScreen())),
            ),
            ListTile(
              leading: const Icon(Icons.download_outlined),
              title: const Text('Download my data'),
              subtitle: const Text('Copy everything held about you'),
              onTap: _downloadMyData,
            ),
            ListTile(
              leading: const Icon(Icons.mail_outline),
              title: const Text('Make a privacy request'),
              subtitle: const Text('Correct, erase or limit use of your data'),
              onTap: _makeRequest,
            ),
            if (_requests.isNotEmpty) ...[
              const _Header('Your requests'),
              for (final r in _requests)
                ListTile(
                  dense: true,
                  leading: Icon(r['status'] == 'resolved' ? Icons.check_circle_outline : Icons.hourglass_empty),
                  title: Text(_kinds[r['kind']] ?? r['kind'] as String),
                  subtitle: Text(r['status'] == 'resolved'
                      ? 'Closed: ${r['outcome']}'
                      : 'Open — answered by ${DateTime.tryParse(r['due_at'] as String)?.toLocal().toString().split(' ').first ?? ''}'),
                ),
            ],
          ],
          const _Header('Security'),
          ListTile(
            leading: Icon(enabled ? Icons.verified_user : Icons.shield_outlined, color: enabled ? Colors.green : null),
            title: const Text('Two-step verification'),
            subtitle: Text(enabled
                ? 'On — ${mfa?['recovery_codes_remaining']} recovery code(s) left'
                : 'Off${required ? ' — required for your role' : ''}. Adds a code from an app on your phone to signing in.'),
            trailing: mfa == null
                ? null
                : (enabled
                    ? null
                    : FilledButton(onPressed: _enableMfa, child: const Text('Set up'))),
          ),
          if (enabled) ...[
            ListTile(
              leading: const Icon(Icons.key_outlined),
              title: const Text('Get new recovery codes'),
              onTap: _newRecoveryCodes,
            ),
            if (!required)
              ListTile(
                leading: const Icon(Icons.shield_outlined),
                title: const Text('Turn off two-step verification'),
                onTap: _disableMfa,
              ),
          ],
        ]),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  final String text;
  const _Header(this.text);

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 20, 16, 4),
        child: Text(text.toUpperCase(),
            style: Theme.of(context).textTheme.labelMedium?.copyWith(letterSpacing: 1, color: Theme.of(context).hintColor)),
      );
}
