import 'dart:async';

import 'package:flutter/material.dart';

import '../models/security.dart';
import '../services/api_client.dart';

/// Security alerts — break-in / after-hours / off-limits / "still inside after closing" — and the alarm's
/// armed state. Mirrors the web dashboard's "Security alerts" panel (see security_engine.py for the rules).
/// A detection means a person was seen on camera in that area at that time and nothing more: the screen says
/// so, because the right response is to look at the cameras, not to assume.
class SecurityAlertsScreen extends StatefulWidget {
  const SecurityAlertsScreen({super.key});

  @override
  State<SecurityAlertsScreen> createState() => _SecurityAlertsScreenState();
}

class _SecurityAlertsScreenState extends State<SecurityAlertsScreen> {
  SecurityStatus? _status;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
    // The armed/open phase changes with the clock, and other supervisors act on alerts too.
    _timer = Timer.periodic(const Duration(seconds: 15), (_) => _load());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final status = await ApiClient.instance.fetchSecurityStatus();
      if (!mounted) return;
      setState(() {
        _status = status;
        _error = null;
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (_) {
      if (mounted) setState(() => _error = 'Network error — pull down to retry.');
    }
  }

  /// Runs one action, reports any failure, and reloads either way so the screen never shows stale state.
  Future<void> _run(Future<void> Function() action) async {
    try {
      await action();
    } on ApiException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('Network error — check your connection and try again.')));
      }
    }
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    final status = _status;
    return Scaffold(
      appBar: AppBar(title: const Text('Security alerts')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: status == null
            ? ListView(children: [
                if (_error != null)
                  Padding(padding: const EdgeInsets.all(24), child: Text(_error!, style: const TextStyle(color: Colors.red)))
                else
                  const Padding(padding: EdgeInsets.all(48), child: Center(child: CircularProgressIndicator())),
              ])
            : SecurityAlertsView(
                status: status,
                error: _error,
                onAcknowledge: (id) => _run(() => ApiClient.instance.acknowledgeSecurityAlert(id)),
                onResolve: (id, outcome, note) => _run(() => ApiClient.instance.resolveSecurityAlert(id, outcome, note)),
                onDisarm: (minutes, reason) => _run(() => ApiClient.instance.disarmSecurity(minutes, reason)),
                onArm: () => _run(() => ApiClient.instance.armSecurity()),
              ),
      ),
    );
  }
}

/// The screen's content, with no networking of its own (so it can be tested with plain callbacks).
class SecurityAlertsView extends StatelessWidget {
  final SecurityStatus status;
  final String? error;
  final Future<void> Function(String alertId) onAcknowledge;
  final Future<void> Function(String alertId, String outcome, String note) onResolve;
  final Future<void> Function(int minutes, String reason) onDisarm;
  final Future<void> Function() onArm;

  const SecurityAlertsView({
    super.key,
    required this.status,
    this.error,
    required this.onAcknowledge,
    required this.onResolve,
    required this.onDisarm,
    required this.onArm,
  });

  Future<void> _resolveDialog(BuildContext context, SecurityAlert alert) async {
    var outcome = 'handled';
    final note = TextEditingController();
    final done = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setState) => AlertDialog(
          title: Text('Close: ${alert.label}'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('What did you find?'),
              RadioGroup<String>(
                groupValue: outcome,
                onChanged: (v) => setState(() => outcome = v ?? outcome),
                child: Column(children: [
                  for (final e in securityOutcomeLabels.entries)
                    RadioListTile<String>(
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      value: e.key,
                      title: Text(e.value),
                    ),
                ]),
              ),
              TextField(
                controller: note,
                maxLength: 500,
                decoration: const InputDecoration(labelText: 'Note (optional)', border: OutlineInputBorder()),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Close alert')),
          ],
        ),
      ),
    );
    if (done == true) await onResolve(alert.alertId, outcome, note.text.trim());
  }

  Future<void> _disarmDialog(BuildContext context) async {
    var minutes = 60;
    final reason = TextEditingController();
    final done = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setState) => AlertDialog(
          title: const Text('Disarm the alarm?'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('Door, window and closed-hours alerts stop until this runs out or you re-arm. '
                  'It is recorded and the team is told. Off-limits areas still alert.'),
              const SizedBox(height: 12),
              DropdownButtonFormField<int>(
                initialValue: minutes,
                decoration: const InputDecoration(labelText: 'For how long?', border: OutlineInputBorder()),
                items: [
                  for (final m in const [30, 60, 120, 240, 480, 720])
                    DropdownMenuItem(value: m, child: Text(m >= 60 ? '${m ~/ 60} h' : '$m min')),
                ],
                onChanged: (v) => setState(() => minutes = v ?? minutes),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: reason,
                maxLength: 120,
                decoration: const InputDecoration(labelText: 'Why? (e.g. cleaning crew)', border: OutlineInputBorder()),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Disarm')),
          ],
        ),
      ),
    );
    if (done == true) await onDisarm(minutes, reason.text.trim());
  }

  @override
  Widget build(BuildContext context) {
    final phaseColor = status.phase == 'armed' ? Colors.red : (status.isDisarmed ? Colors.orange : Colors.green);
    return ListView(padding: const EdgeInsets.all(16), children: [
      if (error != null) Padding(padding: const EdgeInsets.only(bottom: 8), child: Text(error!, style: const TextStyle(color: Colors.red))),
      Text(securityPhaseText[status.phase] ?? status.phase,
          style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16, color: phaseColor)),
      if (status.scheduleError != null)
        Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text('Opening hours could not be read: ${status.scheduleError}', style: const TextStyle(color: Colors.red)),
        )
      else if (!status.scheduleConfigured)
        const Padding(
          padding: EdgeInsets.only(top: 4),
          child: Text('No opening hours set: only off-limits areas can alert.'),
        ),
      if (status.shadowMode)
        const Padding(
          padding: EdgeInsets.only(top: 4),
          child: Text('Shadow mode is ON: alerts appear here but nobody is texted or pushed.',
              style: TextStyle(color: Colors.orange)),
        ),
      const SizedBox(height: 8),
      if (status.isDisarmed)
        Wrap(crossAxisAlignment: WrapCrossAlignment.center, spacing: 12, children: [
          Text('Disarmed by ${status.disarmedBy ?? 'someone'}. Off-limits areas still alert.'),
          FilledButton(onPressed: onArm, child: const Text('Re-arm now')),
        ])
      else if (status.enabled)
        OutlinedButton.icon(
          onPressed: () => _disarmDialog(context),
          icon: const Icon(Icons.shield_outlined),
          label: const Text('Disarm… (cleaners, a delivery)'),
        ),
      if (status.occupiedZones.isNotEmpty)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text('Currently detected in: ${status.occupiedZones.join(', ')}'),
        ),
      const Divider(height: 32),
      Text('Open alerts', style: Theme.of(context).textTheme.titleMedium),
      const SizedBox(height: 4),
      Text(
        'A detection means a person was seen on camera in that area at that time — nothing more. '
        'Check the cameras, then say you are on it, or close the alert with what you found.',
        style: Theme.of(context).textTheme.bodySmall,
      ),
      const SizedBox(height: 8),
      if (status.alerts.isEmpty)
        const Padding(padding: EdgeInsets.symmetric(vertical: 24), child: Center(child: Text('No open alerts.')))
      else
        for (final a in status.alerts) _AlertCard(alert: a, onAcknowledge: onAcknowledge, onClose: () => _resolveDialog(context, a)),
    ]);
  }
}

class _AlertCard extends StatelessWidget {
  final SecurityAlert alert;
  final Future<void> Function(String alertId) onAcknowledge;
  final VoidCallback onClose;

  const _AlertCard({required this.alert, required this.onAcknowledge, required this.onClose});

  @override
  Widget build(BuildContext context) {
    final color = alert.isCritical ? Colors.red : Colors.orange;
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        side: BorderSide(color: color, width: 1.5),
      ),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Text(alert.severity.toUpperCase(), style: TextStyle(color: color, fontWeight: FontWeight.w800, fontSize: 12)),
            const SizedBox(width: 8),
            Expanded(child: Text(alert.label, style: const TextStyle(fontWeight: FontWeight.bold))),
          ]),
          const SizedBox(height: 2),
          Text(
            '${alert.zoneName} · ${alert.isOpen ? (alert.stillPresent ? 'person still detected' : 'no longer detected') : 'acknowledged by ${alert.acknowledgedBy ?? 'someone'}'}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
          const SizedBox(height: 6),
          Text(alert.message),
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 4, children: [
            if (alert.isOpen) OutlinedButton(onPressed: () => onAcknowledge(alert.alertId), child: const Text("I'm on it")),
            FilledButton(onPressed: onClose, child: const Text('Close alert…')),
          ]),
        ]),
      ),
    );
  }
}

/// An app-bar bell that shows how many security alerts are open and opens the screen. Polls quietly: a failed
/// poll just keeps the last count (the screen itself reports errors).
class SecurityAlertsButton extends StatefulWidget {
  const SecurityAlertsButton({super.key});

  @override
  State<SecurityAlertsButton> createState() => _SecurityAlertsButtonState();
}

class _SecurityAlertsButtonState extends State<SecurityAlertsButton> {
  int _open = 0;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _poll();
    _timer = Timer.periodic(const Duration(seconds: 30), (_) => _poll());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _poll() async {
    try {
      final status = await ApiClient.instance.fetchSecurityStatus();
      if (mounted) setState(() => _open = status.openCount);
    } catch (_) {
      // keep the last known count
    }
  }

  @override
  Widget build(BuildContext context) {
    return IconButton(
      tooltip: 'Security alerts',
      icon: Badge(
        isLabelVisible: _open > 0,
        label: Text('$_open'),
        child: Icon(_open > 0 ? Icons.notifications_active : Icons.notifications_none,
            color: _open > 0 ? Colors.red : null),
      ),
      onPressed: () async {
        await Navigator.of(context).push(MaterialPageRoute(builder: (_) => const SecurityAlertsScreen()));
        _poll();
      },
    );
  }
}
