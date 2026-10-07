import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../services/api_client.dart';

/// Asks for a 6-digit authenticator code (or a recovery code) and checks it with
/// [verify] — a server call that returns normally on success and throws on a
/// wrong code. The dialog stays open on a wrong code, showing the message, so
/// the person can try again; it closes with whatever [verify] returned, or null
/// if they cancel.
Future<T?> promptForMfaCode<T>(
  BuildContext context, {
  required Future<T> Function(String code) verify,
  String title = 'Two-step verification',
  String message = 'Enter the 6-digit code from your authenticator app — or a recovery code if you have lost your phone.',
  Widget? extra,
}) {
  final controller = TextEditingController();
  String? error;
  bool busy = false;
  return showDialog<T>(
    context: context,
    barrierDismissible: false,
    builder: (dialogContext) => StatefulBuilder(
      builder: (dialogContext, setState) {
        Future<void> submit() async {
          final code = controller.text.trim();
          if (code.isEmpty || busy) return;
          setState(() {
            busy = true;
            error = null;
          });
          try {
            final result = await verify(code);
            if (dialogContext.mounted) Navigator.of(dialogContext).pop(result);
          } on ApiException catch (e) {
            if (dialogContext.mounted) setState(() => error = e.message);
          } catch (_) {
            if (dialogContext.mounted) setState(() => error = 'Network error — check your connection and try again.');
          } finally {
            if (dialogContext.mounted) setState(() => busy = false);
          }
        }

        return AlertDialog(
          title: Text(title),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(message),
              if (extra != null) ...[const SizedBox(height: 10), extra],
              const SizedBox(height: 12),
              TextField(
                controller: controller,
                autofocus: true,
                autocorrect: false,
                enableSuggestions: false,
                autofillHints: const [AutofillHints.oneTimeCode],
                onSubmitted: (_) => submit(),
                decoration: const InputDecoration(labelText: 'Code', border: OutlineInputBorder()),
              ),
              if (error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(error!, style: const TextStyle(color: Colors.red)),
                ),
            ],
          ),
          actions: [
            TextButton(onPressed: busy ? null : () => Navigator.of(dialogContext).pop(), child: const Text('Cancel')),
            FilledButton(
              onPressed: busy ? null : submit,
              child: busy
                  ? const SizedBox(height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Verify'),
            ),
          ],
        );
      },
    ),
  );
}

/// Shows one-time recovery codes. They are displayed ONCE — there is a Copy
/// button, and the dialog can't be dismissed by tapping outside.
Future<void> showRecoveryCodesDialog(BuildContext context, List<String> codes) {
  return showDialog<void>(
    context: context,
    barrierDismissible: false,
    builder: (dialogContext) => AlertDialog(
      title: const Text('Save your recovery codes'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('If you lose your phone, each of these signs you in once. They are not shown again — '
              'store them somewhere safe (a password manager is ideal).'),
          const SizedBox(height: 12),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: Theme.of(dialogContext).colorScheme.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(8),
            ),
            child: SelectableText(codes.join('\n'), style: const TextStyle(fontFamily: 'monospace', height: 1.6)),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () async {
            await Clipboard.setData(ClipboardData(text: codes.join('\n')));
            if (dialogContext.mounted) {
              ScaffoldMessenger.of(dialogContext).showSnackBar(const SnackBar(content: Text('Copied.')));
            }
          },
          child: const Text('Copy'),
        ),
        FilledButton(onPressed: () => Navigator.of(dialogContext).pop(), child: const Text("I've saved them")),
      ],
    ),
  );
}

/// Walks someone through setting up an authenticator app: shows the key, asks
/// for the first code to prove it works, then shows their recovery codes.
///
/// [begin] starts enrolment on the server and returns {secret, otpauth_uri};
/// [confirm] takes the first code and returns the server's response (which
/// carries `recovery_codes`, and — when this is part of signing in — the
/// session). Returns that response, or null if the person cancelled.
Future<Map<String, dynamic>?> runMfaEnrollment(
  BuildContext context, {
  required Future<Map<String, dynamic>> Function() begin,
  required Future<Map<String, dynamic>> Function(String code) confirm,
  String title = 'Set up two-step verification',
  String? reason,
}) async {
  Map<String, dynamic> started;
  try {
    started = await begin();
  } on ApiException catch (e) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
    return null;
  } catch (_) {
    if (context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Network error — check your connection and try again.')));
    }
    return null;
  }
  if (!context.mounted) return null;
  final secret = (started['secret'] as String).replaceAllMapped(RegExp(r'.{4}'), (m) => '${m.group(0)} ').trim();

  final result = await promptForMfaCode<Map<String, dynamic>>(
    context,
    title: title,
    message: '${reason == null ? '' : '$reason\n\n'}'
        'Add this account to an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, Authy…) '
        'by entering the key below, then type the 6-digit code the app shows.',
    extra: _KeyBox(secret),
    verify: confirm,
  );
  if (result == null) return null;
  final codes = (result['recovery_codes'] as List?)?.cast<String>() ?? const <String>[];
  if (codes.isNotEmpty && context.mounted) await showRecoveryCodesDialog(context, codes);
  return result;
}

/// The authenticator key, selectable, with a Copy button.
class _KeyBox extends StatelessWidget {
  final String secret;
  const _KeyBox(this.secret);

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(12, 4, 4, 4),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(children: [
        Expanded(child: SelectableText(secret, style: const TextStyle(fontFamily: 'monospace', fontSize: 15))),
        IconButton(
          tooltip: 'Copy key',
          icon: const Icon(Icons.copy, size: 18),
          onPressed: () async {
            await Clipboard.setData(ClipboardData(text: secret.replaceAll(' ', '')));
            if (context.mounted) {
              ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Key copied.')));
            }
          },
        ),
      ]),
    );
  }
}
